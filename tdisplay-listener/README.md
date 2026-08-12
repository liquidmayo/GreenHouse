# HGA Listener Counter — LilyGo T-Display S3

Desk display for the HomeGrown Alerts monitor: shows live **rdio-scanner**
and **ThinLine** listener counts, polled from the HGA Monitor master's
`/api/ticker` endpoint every 10 seconds over WiFi.

- **Split view** (default): both counts side by side, HGA green on black
- **Buttons**: KEY (side button) cycles split → rdio zoom → thinline zoom;
  BOOT cycles 4 brightness levels
- Status dot mirrors the dashboard's worst component status
  (green / yellow / red); shows `STALE Ns` if polling stops; auto-reconnects WiFi

## Requirements

- LilyGo T-Display S3 (non-touch or touch, ESP32-S3, 1.9" 170x320)
- USB-C cable, 2.4 GHz WiFi
- The HGA Monitor master running with the `/api/ticker` endpoint (restart
  after updating) and reachable on the LAN
- Python + PlatformIO on the build machine: `pip install platformio`

## Build & flash

1. Copy `include/config.h.example` to `include/config.h` and fill in WiFi
   credentials, the master's LAN URL, and the API key from `monitors.yml`.
2. Plug in the board (shows up as native USB, VID 303A — no drivers needed
   on Windows 11).
3. From this folder:

```bash
pio run -t upload
```

If flashing fails to start, hold the **BOOT** button while plugging the
cable in (forces download mode), then retry.

First boot sequence: splash → "CONNECTING <ssid>" → live numbers.
Serial debug at 115200 baud on the same USB port (`pio device monitor`).

## Distributable firmware (optional)

`make-flashable.ps1` merges the build into a single `merged.bin` flashable
at offset 0 with esptool or ESP Web Tools (chipFamily "ESP32-S3"):

```bash
powershell -File make-flashable.ps1
```

Note: WiFi credentials and the API key are **compiled in**, so a merged
binary is specific to your network — don't publish it.

## Troubleshooting

- **Blank screen but serial works**: GPIO15 (LCD power) — already handled
  first thing in `setup()`; if you modified `main.cpp`, keep that first.
- **Colors look swapped**: flip `-DTFT_RGB_ORDER=TFT_RGB` to `TFT_BGR`
  in `platformio.ini` (panel batches vary).
- **`--` shown for a count**: the master doesn't have that metric yet —
  rdio needs the admin password set in `monitors.yml`, ThinLine needs the
  `/api/health` probe credentials.
- **STALE**: device can't reach the master — check the URL in `config.h`,
  the master is running, and port 8090 is allowed through Windows Firewall.
