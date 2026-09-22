#!/usr/bin/env python3
"""HGA Matrix Ticker — HomeGrown Alerts stats on a 128x64 HUB75 LED matrix.

Hardware: Raspberry Pi 3B+ + triple matrix bonnet + 2x 64x64 panels chained
(128x64), driven with the library's DEFAULT ("regular") wiring mapping.

Normal operation cycles three pages (HGA style: black background, green):
  1. LISTENERS — rdio + ThinLine counts, big
  2. CALLS     — calls/min + last talkgroup scrolling marquee
  3. AUDIENCE  — YouTube viewers + Facebook followers

Tone-out break-in: an MQTT message on the configured topic interrupts the
rotation — the panel strobes red, then shows the alert big (yellow) with
details scrolling beneath for alert_seconds, then the rotation resumes.
Alerts queue FIFO; duplicates within dedupe_seconds are dropped; alerts
override night dimming. The MQTT layer reconnects forever and its failure
never affects the normal display.

Runs as root (matrix GPIO timing); systemd unit: hga-matrix.
"""
import json
import os
import threading
import time
from collections import deque

import requests
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json")))

from rgbmatrix import RGBMatrix, RGBMatrixOptions  # noqa: E402

W, H = 128, 64

# ---- HGA palette ----
GREEN = (34, 197, 94)
GREEN_DIM = (22, 101, 52)
MUTED = (120, 140, 125)
YELLOW = (234, 179, 8)
RED = (239, 68, 68)
GRAY = (70, 80, 72)

FONT_DIR = "/usr/share/fonts/truetype/dejavu"


def font(size):
    return ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", size)


F_BIG = font(26)
F_MED = font(15)
F_SMALL = font(9)

STALE_S = 60
PAGES = 3

# ---- live ticker state (poll thread writes, render loop reads) ----
state = {
    "rdio": None, "thinline": None, "viewers": None, "followers": None,
    "calls_min": None, "tg": "", "tg_age": None, "status": "unk",
}
last_ok = 0.0
lock = threading.Lock()

# ---- alert state (mqtt thread writes, render loop consumes) ----
alerts = deque()
alert_lock = threading.Lock()
_recent_alerts = {}


def poll_loop():
    global last_ok
    sess = requests.Session()
    while True:
        try:
            r = sess.get(CFG["ticker_url"],
                         headers={"X-API-Key": CFG["api_key"]}, timeout=8)
            if r.status_code == 200:
                d = r.json()
                with lock:
                    state["rdio"] = d.get("rdio")
                    state["thinline"] = d.get("thinline")
                    state["viewers"] = d.get("viewers")
                    state["followers"] = d.get("followers")
                    state["calls_min"] = d.get("calls_min")
                    lc = d.get("last_call") or {}
                    state["tg"] = lc.get("talkgroup") or ""
                    state["tg_age"] = lc.get("age_s")
                    state["status"] = d.get("status") or "unk"
                last_ok = time.time()
        except Exception as exc:
            print(f"poll error: {exc}", flush=True)
        time.sleep(CFG.get("poll_seconds", 10))


# ------------------------------------------------------------------ mqtt

def parse_alert(payload):
    """Format-tolerant: JSON with common field names, or raw text."""
    primary, secondary = "", ""
    try:
        d = json.loads(payload)
        if isinstance(d, dict):
            # twotoneproject payloads: description = department/station,
            # transcription = the dispatch audio text (address etc.)
            for k in ("department", "dept", "agency", "name", "title",
                      "tone", "label", "alert", "description"):
                if d.get(k):
                    primary = str(d[k])
                    break
            for k in ("transcription", "message", "text", "detail",
                      "details", "description", "channel", "system",
                      "county"):
                if d.get(k) and str(d[k]) != primary:
                    secondary = str(d[k])
                    break
            if not primary:
                primary = json.dumps(d)[:70]
        else:
            primary = str(d)
    except Exception:
        primary = payload.strip()[:90]
    return {"primary": primary or "ALERT", "secondary": secondary,
            "ts": time.time()}


def mqtt_loop():
    mcfg = CFG.get("mqtt") or {}
    if not mcfg.get("host"):
        print("mqtt: not configured", flush=True)
        return
    import paho.mqtt.client as mqtt

    def on_connect(client, userdata, flags, reason_code, properties=None):
        print(f"mqtt connected ({reason_code}), subscribing "
              f"{mcfg.get('topic')}", flush=True)
        client.subscribe(mcfg.get("topic", "homegrown/alerts"))

    def on_message(client, userdata, msg):
        raw = msg.payload.decode("utf-8", "replace")
        print(f"mqtt message on {msg.topic}: {raw[:300]}", flush=True)
        a = parse_alert(raw)
        now = time.time()
        with alert_lock:
            if now - _recent_alerts.get(a["primary"], 0) < \
                    mcfg.get("dedupe_seconds", 60):
                print(f"alert deduped: {a['primary']}", flush=True)
                return
            _recent_alerts[a["primary"]] = now
            alerts.append(a)

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except (AttributeError, TypeError):   # paho-mqtt 1.x fallback
        client = mqtt.Client()
    if mcfg.get("username"):
        client.username_pw_set(mcfg["username"], mcfg.get("password", ""))
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=2, max_delay=60)
    while True:
        try:
            client.connect(mcfg["host"], int(mcfg.get("port", 1883)),
                           keepalive=30)
            client.loop_forever(retry_first_connection=True)
        except Exception as exc:
            print(f"mqtt error: {exc}", flush=True)
            time.sleep(10)


# ------------------------------------------------------------------ drawing

def status_color(s):
    return {"ok": GREEN, "warn": YELLOW, "crit": RED}.get(s, GRAY)


def is_night():
    h = time.localtime().tm_hour
    ns, ne = CFG.get("night_start", 23), CFG.get("night_end", 7)
    return (ns <= h or h < ne) if ns > ne else (ns <= h < ne)


def fmt(v):
    if v is None:
        return "--"
    v = int(v)
    return f"{v/1000:.1f}k" if v >= 10000 else str(v)


def text_w(draw, s, f):
    return draw.textbbox((0, 0), s, font=f)[2]


def new_frame():
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    draw.fontmode = "1"   # no antialiasing — crisp pixels on the matrix
    return img, draw


def draw_dot(draw, s):
    draw.rectangle((W - 4, 0, W - 1, 3), fill=status_color(s))


def marquee(draw, s, f, y, scroll, fill):
    tw = text_w(draw, s, f)
    if tw <= W - 4:
        draw.text(((W - tw) // 2, y), s, font=f, fill=fill)
        return
    span = tw + 40
    x = -(scroll % span)
    draw.text((x + 2, y), s, font=f, fill=fill)
    draw.text((x + 2 + span, y), s, font=f, fill=fill)


def page_listeners(draw, st):
    draw.text((2, 2), "RDIO", font=F_SMALL, fill=MUTED)
    n = fmt(st["rdio"])
    draw.text((W - 4 - text_w(draw, n, F_BIG), 0), n, font=F_BIG, fill=GREEN)
    draw.line((4, 32, W - 5, 32), fill=GREEN_DIM)
    draw.text((2, 36), "THINLINE", font=F_SMALL, fill=MUTED)
    n = fmt(st["thinline"])
    draw.text((W - 4 - text_w(draw, n, F_BIG), 33), n, font=F_BIG, fill=GREEN)


def page_calls(draw, st, scroll):
    draw.text((2, 2), "CALLS/MIN", font=F_SMALL, fill=MUTED)
    n = fmt(st["calls_min"])
    draw.text((W - 4 - text_w(draw, n, F_BIG), 0), n, font=F_BIG, fill=GREEN)
    draw.line((4, 34, W - 5, 34), fill=GREEN_DIM)
    marquee(draw, st["tg"] or "no recent calls", F_MED, 42, scroll, GREEN)


def page_audience(draw, st):
    draw.text((2, 2), "YT VIEWERS", font=F_SMALL, fill=MUTED)
    n = fmt(st["viewers"])
    draw.text((W - 4 - text_w(draw, n, F_BIG), 0), n, font=F_BIG, fill=GREEN)
    draw.line((4, 32, W - 5, 32), fill=GREEN_DIM)
    draw.text((2, 36), "FB FOLLOWERS", font=F_SMALL, fill=MUTED)
    n = fmt(st["followers"])
    draw.text((W - 4 - text_w(draw, n, F_MED), 40), n, font=F_MED, fill=GREEN)


def marquee_pass_seconds(text, f):
    """Seconds for one full marquee cycle of `text`, 0 if it fits statically.
    Scroll advances 2px per ~0.05s frame; pad 15% for frame-time drift."""
    if not text:
        return 0
    img = Image.new("RGB", (1, 1))
    d = ImageDraw.Draw(img)
    d.fontmode = "1"
    tw = d.textbbox((0, 0), text, font=f)[2]
    if tw <= W - 4:
        return 0
    return (tw + 40) / (2 / 0.05) * 1.15


def page_alert(draw, alert, scroll):
    draw.text((2, 1), "TONE OUT", font=F_SMALL, fill=RED)
    primary = alert["primary"]
    f = None
    for size in (26, 20, 16, 13):
        cand = font(size)
        if text_w(draw, primary, cand) <= W - 4:
            f = cand
            break
    if f:
        tw = text_w(draw, primary, f)
        draw.text(((W - tw) // 2, 16), primary, font=f, fill=YELLOW)
    else:
        marquee(draw, primary, font(16), 18, scroll, YELLOW)
    if alert.get("secondary"):
        sec = alert["secondary"]
        # transcription line in the medium font — it carries the address;
        # bottom-anchor by measured height so descenders stay on-panel
        y = H - draw.textbbox((0, 0), sec, font=F_MED)[3] - 1
        marquee(draw, sec, F_MED, y, scroll, GREEN)


# ------------------------------------------------------------------ main

def main():
    opts = RGBMatrixOptions()
    opts.rows = 64
    opts.cols = 64
    opts.chain_length = 2
    opts.parallel = 1
    opts.hardware_mapping = CFG.get("hardware_mapping", "regular")
    opts.gpio_slowdown = CFG.get("gpio_slowdown", 1)
    opts.brightness = CFG.get("brightness_day", 60)
    opts.drop_privileges = False
    matrix = RGBMatrix(options=opts)
    canvas = matrix.CreateFrameCanvas()

    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=mqtt_loop, daemon=True).start()

    mcfg = CFG.get("mqtt") or {}
    flash_cycles = mcfg.get("flash_count", 3)
    flash_window = flash_cycles * 0.6              # 0.3s on / 0.3s off
    alert_hold = mcfg.get("alert_seconds", 20)

    page = 0
    page_since = time.time()
    scroll = 0
    last_bright_check = 0.0
    current_alert = None
    alert_started = 0.0
    current_hold = alert_hold

    while True:
        now = time.time()

        # ---- alert break-in ----
        if current_alert is None:
            with alert_lock:
                if alerts:
                    current_alert = alerts.popleft()
                    alert_started = now
                    scroll = 0
                    # adaptive hold: never cut off a scrolling transcription
                    current_hold = max(
                        alert_hold,
                        marquee_pass_seconds(current_alert.get("secondary"), F_MED) + 1,
                        marquee_pass_seconds(current_alert.get("primary"), font(16)) + 1)
                    # alerts override night dimming
                    matrix.brightness = CFG.get(
                        "brightness_alert", CFG.get("brightness_day", 60))
                    print(f"ALERT break-in: {current_alert['primary']}",
                          flush=True)
        if current_alert is not None:
            el = now - alert_started
            if el < flash_window:                  # strobe phase
                img, draw = new_frame()
                if int(el / 0.3) % 2 == 0:
                    draw.rectangle((0, 0, W - 1, H - 1), fill=RED)
                canvas.SetImage(img)
                canvas = matrix.SwapOnVSync(canvas)
                time.sleep(0.05)
                continue
            if el < flash_window + current_hold:   # alert page (adaptive)
                img, draw = new_frame()
                page_alert(draw, current_alert, scroll)
                scroll += 2
                canvas.SetImage(img)
                canvas = matrix.SwapOnVSync(canvas)
                time.sleep(0.05)
                continue
            current_alert = None                   # done — resume rotation
            page_since = now
            scroll = 0
            last_bright_check = 0.0                # re-evaluate night dim

        # ---- normal rotation ----
        if now - page_since >= CFG.get("rotate_seconds", 8):
            page = (page + 1) % PAGES
            page_since = now
            scroll = 0

        if now - last_bright_check > 60:
            last_bright_check = now
            matrix.brightness = (CFG.get("brightness_night", 20) if is_night()
                                 else CFG.get("brightness_day", 60))

        with lock:
            st = dict(state)

        img, draw = new_frame()
        if page == 0:
            page_listeners(draw, st)
        elif page == 1:
            page_calls(draw, st, scroll)
            scroll += 2
        else:
            page_audience(draw, st)
        draw_dot(draw, st["status"])
        if last_ok == 0 or now - last_ok > STALE_S:
            draw.rectangle((0, H - 10, 44, H - 1), fill=(0, 0, 0))
            draw.text((2, H - 10), "STALE", font=F_SMALL, fill=YELLOW)

        canvas.SetImage(img)
        canvas = matrix.SwapOnVSync(canvas)
        time.sleep(0.05 if page == 1 else 0.2)


if __name__ == "__main__":
    main()
