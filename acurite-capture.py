#!/usr/bin/env python3
"""
acurite-capture.py — AcuRite weather sensor capture daemon.

Captures AcuRite sensor data via rtl_433, appends readings to weather.csv,
and optionally uploads to Weather Underground.

Requirements:
    sudo apt install rtl-sdr
    sudo apt install rtl-433          # Ubuntu 22.04+
    # or build from source: https://github.com/merbanan/rtl_433

    Add your user to the plugdev group for non-root SDR access:
    sudo usermod -aG plugdev $USER    # log out and back in after

Setup:
    cp config.ini.example ~/.acurite-free/config.ini
    python3 acurite-capture.py --discover   # find your sensor IDs
    # edit config.ini — add sensor IDs under [sensors]
    python3 acurite-capture.py              # run daemon

Usage:
    python3 acurite-capture.py [--config PATH] [--discover] [--discover-time N] [-v]
"""

from __future__ import annotations

import argparse
import configparser
import csv
import json
import logging
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("acurite")

DEFAULT_CONFIG = Path.home() / ".acurite-free" / "config.ini"
DEFAULT_PROTOCOLS = ["40", "78", "112", "191"]
WU_URL = "https://weatherstation.wunderground.com/weatherstation/updateweatherstation.php"

CSV_FIELDS = [
    "timestamp", "sensor_id", "sensor_name", "model",
    "temp_f", "humidity_pct", "wind_mph", "wind_dir_deg",
    "rain_in", "battery_ok",
]


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if not path.exists():
        log.error("Config not found: %s", path)
        log.error("Copy config.ini.example to %s and edit it.", path)
        sys.exit(1)
    cfg.read(path)
    return cfg


def sensor_map(cfg: configparser.ConfigParser) -> dict[str, str]:
    """Return {sensor_id_str: display_name} from [sensors] section."""
    if not cfg.has_section("sensors"):
        return {}
    return {k: v for k, v in cfg.items("sensors")}


def rtl433_cmd(cfg: configparser.ConfigParser) -> list[str]:
    protocols = DEFAULT_PROTOCOLS
    if cfg.has_option("capture", "protocols"):
        protocols = [p.strip() for p in cfg.get("capture", "protocols").split(",")]
    device = cfg.get("capture", "device_index", fallback="0")
    cmd = ["rtl_433", "-d", device, "-F", "json"]
    for p in protocols:
        cmd += ["-R", p]
    return cmd


# ── Packet parsing ────────────────────────────────────────────────────────────

def parse_packet(raw: dict) -> dict | None:
    """Extract standardised fields from an rtl_433 JSON packet.

    Returns None for non-AcuRite packets.
    rtl_433 field names vary across versions and sensor models — handle all known variants.
    """
    model = raw.get("model", "")
    if "acurite" not in model.lower():
        return None

    sensor_id = str(raw.get("id", raw.get("sensor_id", ""))).strip()
    if not sensor_id:
        return None

    # Temperature — prefer Fahrenheit; convert Celsius if that's what we got
    temp_f = raw.get("temperature_F", raw.get("temperature_f"))
    if temp_f is None:
        temp_c = raw.get("temperature_C", raw.get("temperature_c"))
        if temp_c is not None:
            temp_f = round(float(temp_c) * 9 / 5 + 32, 1)

    # Wind speed — prefer mph; convert km/h if needed
    wind_mph = raw.get("wind_avg_mi_h", raw.get("wind_speed_mph", raw.get("wind_avg_mph")))
    if wind_mph is None:
        wind_kph = raw.get("wind_avg_km_h", raw.get("wind_speed_kph"))
        if wind_kph is not None:
            wind_mph = round(float(wind_kph) * 0.621371, 1)

    # Wind direction — degrees
    wind_dir = raw.get("wind_dir_deg", raw.get("wind_direction_deg", raw.get("wind_dir")))

    # Rain — rtl_433 reports cumulative mm; convert to inches
    rain_in = None
    rain_mm = raw.get("rain_mm", raw.get("rain_in_raw"))
    if rain_mm is not None:
        rain_in = round(float(rain_mm) / 25.4, 3)
    elif raw.get("rain_in") is not None:
        rain_in = raw["rain_in"]

    return {
        "sensor_id": sensor_id,
        "model": model,
        "temp_f": temp_f,
        "humidity_pct": raw.get("humidity"),
        "wind_mph": wind_mph,
        "wind_dir_deg": wind_dir,
        "rain_in": rain_in,
        "battery_ok": raw.get("battery_ok", raw.get("battery")),
    }


# ── CSV ───────────────────────────────────────────────────────────────────────

def append_csv(path: Path, row: dict) -> None:
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


# ── Weather Underground ───────────────────────────────────────────────────────

def upload_wu(cfg: configparser.ConfigParser, packet: dict) -> None:
    if not cfg.getboolean("weather_underground", "enabled", fallback=False):
        return
    station_id = cfg.get("weather_underground", "station_id", fallback="").strip()
    station_key = cfg.get("weather_underground", "station_key", fallback="").strip()
    if not station_id or not station_key:
        log.warning("WU upload enabled but station_id / station_key not set in config")
        return

    params: dict[str, str] = {
        "ID": station_id,
        "PASSWORD": station_key,
        "dateutc": "now",
        "action": "updateraw",
    }
    if packet.get("temp_f") is not None:
        params["tempf"] = str(round(float(packet["temp_f"]), 1))
    if packet.get("humidity_pct") is not None:
        params["humidity"] = str(int(packet["humidity_pct"]))
    if packet.get("wind_mph") is not None:
        params["windspeedmph"] = str(round(float(packet["wind_mph"]), 1))
    if packet.get("wind_dir_deg") is not None:
        params["winddir"] = str(int(packet["wind_dir_deg"]))
    if packet.get("rain_in") is not None:
        params["rainin"] = str(round(float(packet["rain_in"]), 3))

    url = WU_URL + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            log.info("WU upload OK: %s", resp.read().decode().strip())
    except Exception as exc:
        log.warning("WU upload failed: %s", exc)


# ── Discover mode ─────────────────────────────────────────────────────────────

def run_discover(cfg: configparser.ConfigParser, duration_s: int = 300) -> None:
    """Print all AcuRite sensors in range for duration_s seconds then exit."""
    cmd = rtl433_cmd(cfg)
    print(f"\n{'─'*60}")
    print(f"DISCOVER MODE — listening {duration_s}s for AcuRite sensors")
    print(f"rtl_433: {' '.join(cmd)}")
    print(f"{'─'*60}\n")

    deadline = time.monotonic() + duration_s
    seen: dict[str, dict] = {}

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        )
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            packet = parse_packet(raw)
            if packet is None:
                continue
            sid = packet["sensor_id"]
            if sid not in seen:
                seen[sid] = packet
                print(f"  Sensor ID : {sid}")
                print(f"  Model     : {packet['model']}")
                if packet.get("temp_f") is not None:
                    print(f"  Temp      : {packet['temp_f']}°F")
                if packet.get("humidity_pct") is not None:
                    print(f"  Humidity  : {packet['humidity_pct']}%")
                if packet.get("wind_mph") is not None:
                    print(f"  Wind      : {packet['wind_mph']} mph @ {packet.get('wind_dir_deg', '?')}°")
                if packet.get("rain_in") is not None:
                    print(f"  Rain      : {packet['rain_in']}\"")
                print()
    except KeyboardInterrupt:
        print("\n(stopped early)")
    finally:
        proc.terminate()
        proc.wait()

    print(f"{'─'*60}")
    print(f"Found {len(seen)} sensor(s). Add to ~/.acurite-free/config.ini under [sensors]:\n")
    for sid, p in seen.items():
        print(f"  {sid} = My {p['model']}")
    print(f"\n{'─'*60}\n")


# ── Daemon ────────────────────────────────────────────────────────────────────

def run_daemon(cfg: configparser.ConfigParser) -> None:
    write_path = Path(cfg.get("device", "write_path")).expanduser()
    write_path.mkdir(parents=True, exist_ok=True)
    csv_path = write_path / "weather.csv"

    sensors = sensor_map(cfg)
    whitelist = set(sensors.keys()) if sensors else None

    cmd = rtl433_cmd(cfg)
    log.info("Starting — writing to %s", csv_path)
    if whitelist:
        log.info("Sensor whitelist: %s", sorted(whitelist))
    else:
        log.info("No [sensors] configured — capturing all AcuRite sensors (consider adding IDs to suppress neighbor noise)")

    while True:
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
            )
            log.info("rtl_433 started (pid %d)", proc.pid)
            for line in proc.stdout:
                try:
                    raw = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue

                packet = parse_packet(raw)
                if packet is None:
                    continue

                sid = packet["sensor_id"]
                if whitelist and sid not in whitelist:
                    log.debug("Skipping unknown sensor %s", sid)
                    continue

                now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                row = {
                    "timestamp": now,
                    "sensor_id": sid,
                    "sensor_name": sensors.get(sid, sid),
                    "model": packet["model"],
                    "temp_f": packet.get("temp_f"),
                    "humidity_pct": packet.get("humidity_pct"),
                    "wind_mph": packet.get("wind_mph"),
                    "wind_dir_deg": packet.get("wind_dir_deg"),
                    "rain_in": packet.get("rain_in"),
                    "battery_ok": packet.get("battery_ok"),
                }
                append_csv(csv_path, row)
                log.info(
                    "sensor=%s name=%r temp_f=%s humidity=%s wind_mph=%s",
                    sid, sensors.get(sid, sid),
                    packet.get("temp_f"), packet.get("humidity_pct"), packet.get("wind_mph"),
                )

                threading.Thread(
                    target=upload_wu, args=(cfg, packet), daemon=True
                ).start()

            ret = proc.wait()
            log.warning("rtl_433 exited (code %d) — restarting in 10s", ret)
            time.sleep(10)

        except Exception as exc:
            log.error("Daemon error: %s — restarting in 10s", exc)
            time.sleep(10)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AcuRite weather sensor capture — writes weather.csv for cloud sync"
    )
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG,
        help=f"Path to config.ini (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--discover", action="store_true",
        help="Print all AcuRite sensors in range for --discover-time seconds and exit",
    )
    parser.add_argument(
        "--discover-time", type=int, default=300,
        metavar="SECONDS",
        help="How long to listen in discover mode (default: 300)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cfg = load_config(args.config)

    if args.discover:
        run_discover(cfg, args.discover_time)
    else:
        run_daemon(cfg)


if __name__ == "__main__":
    main()
