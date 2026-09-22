# Matrix Ticker — LED matrix stats display with MQTT tone-out break-in

Python daemon for a Raspberry Pi driving a 128x64 HUB75 LED matrix
(2x 64x64 panels chained) via hzeller/rpi-rgb-led-matrix. Polls the
monitor master's `/api/ticker` and cycles three pages: listeners, calls
(+ scrolling last talkgroup), audience. An MQTT message on the configured
topic breaks in: red strobe, then the alert big with details scrolling,
then the rotation resumes. Alerts queue, dedupe, and override night
dimming; MQTT failure never affects the normal display.

## Setup (on the Pi)

```
sudo apt install git make g++ cmake python3-dev python3-pil python3-venv fonts-dejavu-core
git clone --depth 1 https://github.com/hzeller/rpi-rgb-led-matrix.git ~/rpi-rgb-led-matrix
mkdir -p ~/hga-matrix && python3 -m venv --system-site-packages ~/hga-matrix/venv
~/hga-matrix/venv/bin/pip install ~/rpi-rgb-led-matrix requests paho-mqtt
```

Note: the matrix library now installs with `pip install <repo-root>`
(pyproject) — the old `make build-python` no longer exists.

Copy `matrix_ticker.py` + `config.json` (from `config.example.json`) to
`~/hga-matrix/`, adjust `hga-matrix.service` paths for your user, then:

```
sudo cp hga-matrix.service /etc/systemd/system/
sudo systemctl enable --now hga-matrix
```

## Config notes

- `hardware_mapping`/`gpio_slowdown`: whatever works with the library's
  demo on YOUR bonnet — if the panels stay dark, run
  `examples-api-use/demo -D0 ...` and try `regular` (default),
  `adafruit-hat`, `adafruit-hat-pwm`, and `--led-panel-type=FM6126A`.
- `mqtt`: leave `host` empty to disable alerts entirely.
- Test an alert:
  `mosquitto_pub -h HOST -u USER -P PASS -t your/topic -m '{"department":"TEST","description":"test"}'`
