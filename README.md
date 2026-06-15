# acurite-free

Free, open-source replacement for the AcuRite app. Captures your AcuRite weather sensor data directly over the air using a $20 RTL-SDR dongle — no AcuRite hub, no AcuRite account, no cloud subscription.

Data lives in a CSV file you sync to any cloud storage (OneDrive, Google Drive, Dropbox, iCloud, etc.). A single HTML file served from that same folder is your dashboard — no web server, no infrastructure, just a URL.

## What you need

- **RTL-SDR dongle** — any RTL2832U-based USB dongle (~$20–$30). [RTL-SDR Blog V3](https://www.rtl-sdr.com/buy-rtl-sdr-dvb-t-dongles/) is recommended.
- **A computer that stays on** — Raspberry Pi, NAS, old laptop, the box running your cloud sync client
- **Python 3.8+** — comes with most Linux distros
- **rtl_433** — the open-source SDR decoder

## Installation

### 1. Install rtl_433

```bash
sudo apt install rtl-sdr

# Ubuntu 22.04+ / Debian 12+:
sudo apt install rtl-433

# Older systems — build from source:
# https://github.com/merbanan/rtl_433#installation
```

### 2. Allow non-root SDR access

```bash
sudo usermod -aG plugdev $USER
# Log out and back in for this to take effect
```

Test the dongle:
```bash
rtl_433 -T 5    # should print received packets for 5 seconds; Ctrl+C to stop
```

### 3. Configure

```bash
mkdir -p ~/.acurite-free
cp config.ini.example ~/.acurite-free/config.ini
```

Edit `~/.acurite-free/config.ini` — set `write_path` to a folder your cloud sync client watches.

### 4. Discover your sensor IDs

```bash
python3 acurite-capture.py --discover
```

Leave it running for a few minutes. Your sensors transmit every 18–36 seconds. The output shows each sensor's ID and what it's reporting. Add those IDs to `~/.acurite-free/config.ini` under `[sensors]`:

```ini
[sensors]
1234 = Backyard
5678 = Garage
```

IDs keep noise from your neighbours' sensors out of your CSV.

### 5. Copy weather.html to your synced folder

Put `weather.html` in the same folder as your `write_path` (the one your cloud client syncs). It will read `weather.csv` from the same location.

Share that folder publicly in your cloud storage app and note the public URL.

### 6. Run the daemon

```bash
python3 acurite-capture.py
```

Or install as a systemd service:

```bash
# Edit weather_monitor.service — update the ExecStart path to match your setup
sudo cp weather_monitor.service /etc/systemd/system/acurite@.service
sudo systemctl enable --now acurite@$USER
sudo systemctl status acurite@$USER
```

### 7. Open your dashboard

Navigate to the public URL of your cloud-shared folder and open `weather.html`. Bookmark it. On Android, use **Add to Home Screen** to install it as an app.

## Weather Underground upload

Set `enabled = true` under `[weather_underground]` in config.ini and add your station ID and key. The daemon uploads on every valid sensor reading.

## Files

| File | Purpose |
|---|---|
| `acurite-capture.py` | Capture daemon — the only thing that runs on your machine |
| `config.ini.example` | Configuration template |
| `weather.html` | Dashboard — goes in your cloud-synced folder alongside weather.csv |
| `weather_monitor.service` | systemd unit for auto-start on boot |

## Cloud storage notes

The dashboard fetches `weather.csv` via JavaScript. For this to work, your cloud storage's public share URL must respond with a CORS header (`Access-Control-Allow-Origin: *`). Services that work well:

- **Cloudflare R2** (free tier, CORS configurable in dashboard) — recommended
- **AWS S3** (cheap, CORS configurable)
- **Backblaze B2** (free tier with CORS)
- **OneDrive** — public share links work with fetch() in most browsers
- **Google Drive** — public file URLs may require CORS proxy; Google Sheets export is CORS-friendly

If your cloud storage blocks cross-origin requests, the dashboard will show an error in the browser console. Switch services or add a free CORS proxy in front.

## Supported AcuRite devices

Any device decoded by rtl_433 under AcuRite protocols. Tested with:
- AcuRite 5-in-1 Weather Station (temp, humidity, wind, rain)
- AcuRite Atlas
- AcuRite 592TXR Tower sensor (temp, humidity)

If your device shows up in `--discover` mode, it works.

## License

MIT
