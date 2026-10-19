#!/usr/bin/env python3
"""HGA Matrix Ticker — HomeGrown Alerts stats on a 128x64 HUB75 LED matrix.

Hardware: Raspberry Pi 3B+ + triple matrix bonnet + 2x 64x64 panels chained
(128x64), driven with the library's DEFAULT ("regular") wiring mapping.

Normal operation slides through pages (HGA style: black background, green):
  LISTENERS  — rdio (with 6h sparkline) + ThinLine counts
  YOUTUBE    — watching-now with LIVE chip, or OFFLINE
  FB TOTAL   — total followers + today's gain
  FB PAGES   — two pages per panel, each with today's gain
  LAST TONE  — most recent tone-out + how long ago (once one has fired)
  CLOCK      — time + date

Overlays on every page: status dot (top right), page-position dots (bottom
center), WARN/CRIT component banner (bottom right), STALE (bottom left).

Tone-out break-in: an MQTT message on the configured topic interrupts the
rotation — the panel strobes red, then shows the alert big (yellow) with
details scrolling beneath for alert_seconds, then the rotation resumes.
Alerts queue FIFO; duplicates within dedupe_seconds are dropped; alerts
override night dimming. The MQTT layer reconnects forever and its failure
never affects the normal display. The last alert is persisted to
last_alert.json so the LAST TONE page survives restarts.

Set "night_clock_only": true in config.json to show only the clock page
during night hours instead of the dimmed rotation.

Runs as root (matrix GPIO timing); systemd unit: hga-matrix.
"""
import json
import os
import socket
import threading
import time
from collections import deque

import requests
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json")))

try:
    from rgbmatrix import RGBMatrix, RGBMatrixOptions
except ImportError:      # allows importing page renderers off-Pi for tests
    RGBMatrix = RGBMatrixOptions = None

W, H = 128, 64

# ---- HGA palette ----
GREEN = (34, 197, 94)
GREEN_DIM = (22, 101, 52)
MUTED = (120, 140, 125)
YELLOW = (234, 179, 8)
RED = (239, 68, 68)
GRAY = (70, 80, 72)
WHITE = (255, 255, 255)
FB_BLUE = (24, 119, 242)

FONT_DIR = "/usr/share/fonts/truetype/dejavu"


def font(size):
    try:
        return ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", size)
    except OSError:        # off-Pi test rendering (Windows dev box)
        return ImageFont.truetype("arialbd.ttf", size)


F_BIG = font(26)
F_MED = font(15)
F_SMALL = font(9)
F_CLOCK = font(32)

STALE_S = 60

# ---- live ticker state (poll thread writes, render loop reads) ----
state = {
    "rdio": None, "thinline": None, "viewers": None, "followers": None,
    "fb_pages": [], "spark": [], "worst": None, "nws": [], "status": "unk",
}
last_ok = 0.0
lock = threading.Lock()

# ---- alert state (mqtt thread writes, render loop consumes) ----
alerts = deque()
alert_lock = threading.Lock()
_recent_alerts = {}

LAST_ALERT_FILE = os.path.join(HERE, "last_alert.json")
last_alert = None
try:
    last_alert = json.load(open(LAST_ALERT_FILE))
except Exception:
    pass


def save_last_alert(a):
    try:
        json.dump(a, open(LAST_ALERT_FILE, "w"))
    except Exception as exc:
        print(f"last_alert save failed: {exc}", flush=True)


# NWS alert ids already seen — persisted so a service restart during an
# active warning doesn't replay its break-in
NWS_SEEN_FILE = os.path.join(HERE, "nws_seen.json")
try:
    _nws_seen = json.load(open(NWS_SEEN_FILE))
except Exception:
    _nws_seen = {}


def handle_nws_breakins(nws):
    """Queue a break-in for each never-seen NWS *Warning* (watches only get
    their rotation page). Does not touch last_alert — that stays tone-outs."""
    now = time.time()
    changed = False
    for a in nws:
        aid = a.get("id") or a.get("event", "")
        if not aid or aid in _nws_seen:
            continue
        _nws_seen[aid] = now
        changed = True
        if a.get("event", "").endswith("Warning"):
            alert = {"primary": a.get("event", "NWS Warning"),
                     "secondary": a.get("headline") or a.get("counties", ""),
                     "kind": "nws", "ts": now}
            with alert_lock:
                alerts.append(alert)
            print(f"NWS break-in: {alert['primary']}", flush=True)
    if changed:
        for aid, ts in list(_nws_seen.items()):   # prune after 48h
            if now - ts > 48 * 3600:
                del _nws_seen[aid]
        try:
            json.dump(_nws_seen, open(NWS_SEEN_FILE, "w"))
        except Exception as exc:
            print(f"nws_seen save failed: {exc}", flush=True)


# ---- network self-heal (added after the Pi silently fell off the LAN) ----
# If polls fail for 10 min AND the canary host (the MQTT broker, a different
# machine than the dashboard) is also unreachable, the network itself is dead:
# kick the network stack, and reboot 10 min later if that didn't fix it.
# A dashboard-only outage (canary still reachable) just shows STALE.
BOOT_TS = time.time()
_heal = {"kicked": 0.0}


def network_dead():
    host = CFG.get("canary_host") or (CFG.get("mqtt") or {}).get("host")
    if not host:
        return False
    port = int(CFG.get("canary_port") or (CFG.get("mqtt") or {}).get("port", 1883))
    try:
        socket.create_connection((host, port), timeout=3).close()
        return False
    except OSError:
        return True


def self_heal_check():
    if not CFG.get("self_heal", True):
        return
    now = time.time()
    if now - (last_ok or BOOT_TS) < 600:
        return
    if not network_dead():
        return
    if not _heal["kicked"]:
        _heal["kicked"] = now
        print("self-heal: no data for 10 min and canary unreachable — "
              "restarting network stack", flush=True)
        os.system("systemctl restart NetworkManager 2>/dev/null || "
                  "systemctl restart dhcpcd 2>/dev/null || "
                  "systemctl restart networking 2>/dev/null")
        return
    if now - _heal["kicked"] > 600:
        print("self-heal: still dead 10 min after network restart — "
              "rebooting", flush=True)
        os.system("sync; /sbin/reboot")


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
                    state["fb_pages"] = d.get("followers_pages") or []
                    state["spark"] = d.get("spark") or []
                    state["worst"] = d.get("worst_label")
                    state["nws"] = d.get("nws") or []
                    state["status"] = d.get("status") or "unk"
                last_ok = time.time()
                _heal["kicked"] = 0.0
                handle_nws_breakins(d.get("nws") or [])
        except Exception as exc:
            print(f"poll error: {exc}", flush=True)
        self_heal_check()
        time.sleep(CFG.get("poll_seconds", 10))


# ------------------------------------------------------------------ mqtt

def parse_alert(payload):
    """Format-tolerant: JSON with common field names, or raw text."""
    primary, secondary = "", ""
    enrich = False
    address = ""
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
            # North/Miami Valley twotone enrichment shape: just a geocoded
            # {"call_type": ..., "address": ...} that follows the main
            # tone_detected for the same incident a few seconds later
            if not primary and (d.get("call_type") or d.get("address")):
                enrich = True
                address = str(d.get("address") or "")
                primary = str(d.get("call_type") or "CALL").upper()
                secondary = address
            if not primary:
                primary = json.dumps(d)[:70]
        else:
            primary = str(d)
    except Exception:
        primary = payload.strip()[:90]
    return {"primary": primary or "ALERT", "secondary": secondary,
            "enrich": enrich, "address": address, "ts": time.time()}


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
        global last_alert
        raw = msg.payload.decode("utf-8", "replace")
        print(f"mqtt message on {msg.topic}: {raw[:300]}", flush=True)
        a = parse_alert(raw)
        now = time.time()
        with alert_lock:
            # an address-only enrichment for an incident that just toned:
            # attach the clean address to that alert, no second break-in
            if a["enrich"] and last_alert and \
                    now - last_alert.get("ts", 0) < 180:
                last_alert["address"] = a["address"]
                save_last_alert(last_alert)
                print(f"enrichment merged: {a['address']}", flush=True)
                return
            if now - _recent_alerts.get(a["primary"], 0) < \
                    mcfg.get("dedupe_seconds", 60):
                print(f"alert deduped: {a['primary']}", flush=True)
                return
            _recent_alerts[a["primary"]] = now
            alerts.append(a)
            last_alert = a
        save_last_alert(a)

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
    return str(int(v))   # always the full number


def fmt_age(s):
    s = int(s)
    if s < 60:
        return "now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m ago"
    return f"{s // 86400}d ago"


def fmt_delta(d):
    """(text, color) for a today-gain figure, or None when nothing to show."""
    if not isinstance(d, (int, float)) or int(d) == 0:
        return None
    d = int(d)
    return (f"▲{d}", YELLOW) if d > 0 else (f"▼{-d}", RED)


def text_w(draw, s, f):
    return draw.textbbox((0, 0), s, font=f)[2]


def truncate(draw, s, f, maxw):
    while s and text_w(draw, s, f) > maxw:
        s = s[:-1]
    return s


def fit_font(draw, s, sizes):
    """Largest of `sizes` that fits `s` on one line, else None."""
    for size in sizes:
        f = font(size)
        if text_w(draw, s, f) <= W - 4:
            return f
    return None


def new_frame():
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    draw.fontmode = "1"   # no antialiasing — crisp pixels on the matrix
    return img, draw


def marquee(draw, s, f, y, scroll, fill):
    tw = text_w(draw, s, f)
    if tw <= W - 4:
        draw.text(((W - tw) // 2, y), s, font=f, fill=fill)
        return
    span = tw + 40
    x = -int(scroll % span)   # scroll_px may be fractional
    draw.text((x + 2, y), s, font=f, fill=fill)
    draw.text((x + 2 + span, y), s, font=f, fill=fill)


# ---- tiny pixel icons (drawn, not fonts) ----

def icon_antenna(draw, x, y, color=GREEN):
    draw.line((x + 4, y + 1, x + 4, y + 8), fill=color)
    draw.line((x + 1, y + 8, x + 4, y + 4), fill=color)
    draw.line((x + 7, y + 8, x + 4, y + 4), fill=color)
    draw.point((x + 4, y), fill=YELLOW)
    draw.point((x + 1, y + 1), fill=color)
    draw.point((x + 7, y + 1), fill=color)
    draw.point((x, y + 3), fill=color)
    draw.point((x + 8, y + 3), fill=color)


def icon_speaker(draw, x, y, color=GREEN):
    draw.rectangle((x, y + 3, x + 2, y + 6), fill=color)
    draw.polygon([(x + 2, y + 3), (x + 5, y), (x + 5, y + 9),
                  (x + 2, y + 6)], fill=color)
    draw.point((x + 7, y + 3), fill=color)
    draw.point((x + 8, y + 4), fill=color)
    draw.point((x + 8, y + 5), fill=color)
    draw.point((x + 7, y + 6), fill=color)


def icon_yt(draw, x, y):
    draw.rectangle((x, y + 1, x + 10, y + 7), fill=RED)
    draw.polygon([(x + 4, y + 2), (x + 4, y + 6), (x + 7, y + 4)],
                 fill=WHITE)


def icon_fb(draw, x, y):
    draw.rectangle((x, y, x + 8, y + 8), fill=FB_BLUE)
    draw.text((x + 3, y - 1), "f", font=F_SMALL, fill=WHITE)


def icon_warn(draw, x, y):
    draw.polygon([(x + 5, y), (x, y + 9), (x + 10, y + 9)], fill=YELLOW)
    draw.line((x + 5, y + 3, x + 5, y + 6), fill=(0, 0, 0))
    draw.point((x + 5, y + 8), fill=(0, 0, 0))


def draw_spark(draw, pts, x0, y0, x1, y1):
    """Tiny 6h trend line in the given box (dim green, newest point bright)."""
    if not pts or len(pts) < 2:
        return
    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or 1
    n = len(pts)
    xy = [(x0 + i * (x1 - x0) / (n - 1),
           y1 - (v - lo) * (y1 - y0) / rng) for i, v in enumerate(pts)]
    draw.line(xy, fill=GREEN_DIM, width=1)
    draw.point((int(xy[-1][0]), int(xy[-1][1])), fill=GREEN)


# ---- pages ----

def page_listeners(draw, st):
    icon_antenna(draw, 2, 1)
    draw.text((13, 2), "RDIO", font=F_SMALL, fill=MUTED)
    n = fmt(st["rdio"])
    draw.text((W - 4 - text_w(draw, n, F_BIG), 0), n, font=F_BIG, fill=GREEN)
    draw_spark(draw, st.get("spark"), 2, 15, 56, 30)
    draw.line((4, 32, W - 5, 32), fill=GREEN_DIM)
    icon_speaker(draw, 2, 34)
    draw.text((13, 36), "THINLINE", font=F_SMALL, fill=MUTED)
    n = fmt(st["thinline"])
    draw.text((W - 4 - text_w(draw, n, F_BIG), 33), n, font=F_BIG, fill=GREEN)


def page_youtube(draw, st):
    icon_yt(draw, 2, 1)
    draw.text((16, 2), "YOUTUBE", font=F_SMALL, fill=MUTED)
    if st["viewers"] is None:
        lbl = "OFFLINE"
        draw.text(((W - text_w(draw, lbl, F_MED)) // 2, 24), lbl,
                  font=F_MED, fill=GRAY)
        return
    live = "LIVE"
    draw.text((W - 8 - text_w(draw, live, F_SMALL), 2), live,
              font=F_SMALL, fill=RED)
    n = fmt(st["viewers"])
    draw.text(((W - text_w(draw, n, F_BIG)) // 2, 16), n,
              font=F_BIG, fill=GREEN)
    lbl = "WATCHING NOW"
    draw.text(((W - text_w(draw, lbl, F_SMALL)) // 2, 52), lbl,
              font=F_SMALL, fill=MUTED)


def page_fb(draw, name, count, delta=None):
    """Single FB panel (used for the TOTAL): name + full count + today gain."""
    icon_fb(draw, 2, 1)
    draw.text((14, 2), "FB FOLLOWERS", font=F_SMALL, fill=MUTED)
    dl = fmt_delta(delta)
    if dl:
        draw.text((W - 8 - text_w(draw, dl[0], F_SMALL), 2), dl[0],
                  font=F_SMALL, fill=dl[1])
    nm = name.upper()
    f_name = F_MED if text_w(draw, nm, F_MED) <= W - 4 else F_SMALL
    draw.text(((W - text_w(draw, nm, f_name)) // 2, 14), nm,
              font=f_name, fill=YELLOW)
    n = fmt(count)
    draw.text(((W - text_w(draw, n, F_BIG)) // 2, 32), n,
              font=F_BIG, fill=GREEN)


def fb_row(draw, y, pg):
    """Half-height row: name (+ today gain) left, full count right."""
    nm = str(pg.get("name", "?")).upper()
    n = fmt(pg.get("count"))
    f_num = F_BIG if (text_w(draw, nm, F_SMALL) +
                      text_w(draw, n, F_BIG) + 10 <= W - 6) else F_MED
    dl = fmt_delta(pg.get("delta"))
    if dl:
        draw.text((2, y + 6), nm, font=F_SMALL, fill=MUTED)
        draw.text((2, y + 17), dl[0], font=F_SMALL, fill=dl[1])
    else:
        draw.text((2, y + 11), nm, font=F_SMALL, fill=MUTED)
    h = draw.textbbox((0, 0), n, font=f_num)[3]
    draw.text((W - 4 - text_w(draw, n, f_num), y + (32 - h) // 2),
              n, font=f_num, fill=GREEN)


def page_fb_group(draw, group):
    """Two FB pages on one panel, split top/bottom like the listeners page."""
    fb_row(draw, 0, group[0])
    if len(group) > 1:
        draw.line((4, 32, W - 5, 32), fill=GREEN_DIM)
        fb_row(draw, 32, group[1])


def split2(draw, s, f):
    """Split `s` into two lines at the word gap nearest the middle."""
    words = s.split()
    if len(words) < 2:
        return [s]
    best, best_diff = 1, 10 ** 9
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        diff = abs(text_w(draw, a, f) - text_w(draw, b, f))
        if diff < best_diff:
            best, best_diff = i, diff
    return [" ".join(words[:best]), " ".join(words[best:])]


def page_nws(draw, a, now):
    icon_warn(draw, 2, 1)
    draw.text((16, 2), "NWS ALERT", font=F_SMALL, fill=MUTED)
    event = str(a.get("event", "")).upper()
    color = RED if event.endswith("WARNING") else YELLOW
    f = fit_font(draw, event, (16, 13))
    if f:
        draw.text(((W - text_w(draw, event, f)) // 2, 15), event,
                  font=f, fill=color)
    else:
        lines = split2(draw, event, F_SMALL)
        lf = font(11)
        if any(text_w(draw, ln, lf) > W - 4 for ln in lines):
            lf = F_SMALL
        for i, ln in enumerate(lines):
            ln = truncate(draw, ln, lf, W - 4)
            draw.text(((W - text_w(draw, ln, lf)) // 2, 13 + i * 12), ln,
                      font=lf, fill=color)
    counties = [c.strip() for c in str(a.get("counties", "")).split(",")
                if c.strip()]
    if counties:
        dropped = 0    # drop whole counties, not letters, when too wide
        while len(counties) > 1:
            s = ", ".join(counties).upper() + (f" +{dropped}" if dropped else "")
            if text_w(draw, s, F_SMALL) <= W - 4:
                break
            counties.pop()
            dropped += 1
        s = ", ".join(counties).upper() + (f" +{dropped}" if dropped else "")
        s = truncate(draw, s, F_SMALL, W - 4)
        draw.text(((W - text_w(draw, s, F_SMALL)) // 2, 39), s,
                  font=F_SMALL, fill=GREEN)
    exp = a.get("expires_ts")
    if isinstance(exp, (int, float)) and exp > now:
        s = "UNTIL " + time.strftime("%I:%M %p",
                                     time.localtime(exp)).lstrip("0")
        draw.text(((W - text_w(draw, s, F_SMALL)) // 2, 50), s,
                  font=F_SMALL, fill=MUTED)


def page_last_tone(draw, now):
    la = last_alert or {}
    draw.text((2, 1), "LAST TONE", font=F_SMALL, fill=RED)
    primary = la.get("primary", "")
    addr = la.get("address") or ""
    # with a geocoded address a third line squeezes in: shrink the layout
    y_p, sizes, y_age = ((13, (18, 16, 13, 10), 48) if addr
                         else (18, (20, 16, 13, 10), 44))
    f = fit_font(draw, primary, sizes)
    if f:
        draw.text(((W - text_w(draw, primary, f)) // 2, y_p), primary,
                  font=f, fill=YELLOW)
    else:
        s = truncate(draw, primary, F_SMALL, W - 4)
        draw.text(((W - text_w(draw, s, F_SMALL)) // 2, y_p + 5), s,
                  font=F_SMALL, fill=YELLOW)
    if addr:
        s = truncate(draw, addr, F_SMALL, W - 4)
        draw.text(((W - text_w(draw, s, F_SMALL)) // 2, 35), s,
                  font=F_SMALL, fill=GREEN)
    age = fmt_age(now - la.get("ts", now))
    draw.text(((W - text_w(draw, age, F_SMALL)) // 2, y_age), age,
              font=F_SMALL, fill=MUTED)


def page_clock(draw):
    t = time.localtime()
    hh = time.strftime("%I:%M", t).lstrip("0") or "0:00"
    ap = time.strftime("%p", t)
    tw = text_w(draw, hh, F_CLOCK)
    aw = text_w(draw, ap, F_SMALL)
    x = (W - tw - aw - 4) // 2
    draw.text((x, 8), hh, font=F_CLOCK, fill=GREEN)
    draw.text((x + tw + 4, 30), ap, font=F_SMALL, fill=MUTED)
    d = time.strftime("%a %b %d").upper()
    draw.text(((W - text_w(draw, d, F_SMALL)) // 2, 48), d,
              font=F_SMALL, fill=MUTED)


def marquee_pass_seconds(text, f, px_per_frame=2):
    """Seconds for one full marquee cycle of `text`, 0 if it fits statically.
    Scroll advances px_per_frame per ~0.05s frame; pad 15% for drift."""
    if not text:
        return 0
    img = Image.new("RGB", (1, 1))
    d = ImageDraw.Draw(img)
    d.fontmode = "1"
    tw = d.textbbox((0, 0), text, font=f)[2]
    if tw <= W - 4:
        return 0
    return (tw + 40) / (px_per_frame / 0.05) * 1.15


def page_alert(draw, alert, scroll):
    hdr = "NWS WARNING" if alert.get("kind") == "nws" else "TONE OUT"
    draw.text((2, 1), hdr, font=F_SMALL, fill=RED)
    primary = alert["primary"]
    f = fit_font(draw, primary, (26, 20, 16, 13))
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


# ---- page rotation, overlays, transitions ----

def page_list(st):
    entries = [("listeners", None)]
    for a in st.get("nws", [])[:4]:     # active watches/warnings up front
        entries.append(("nws", a))
    entries += [("youtube", None), ("fb_total", None)]
    fb = st["fb_pages"]
    for i in range(0, len(fb), 2):
        entries.append(("fb_group", fb[i:i + 2]))
    if last_alert:
        entries.append(("last_tone", None))
    entries.append(("clock", None))
    return entries


def draw_overlays(draw, st, idx, total, now):
    draw.rectangle((W - 4, 0, W - 1, 3), fill=status_color(st["status"]))
    if total > 1:                      # page-position dots, bottom center
        span = 5
        x0 = (W - total * span) // 2
        for i in range(total):
            x = x0 + i * span
            if i == idx:
                draw.rectangle((x, 61, x + 1, 62), fill=GREEN)
            else:
                draw.point((x, 62), fill=GRAY)
    # banner only for CRIT — warns stay on the dashboard/Discord, not here
    if st["status"] == "crit" and st.get("worst"):
        txt = truncate(draw, f"{st['status']} {st['worst']}".upper(),
                       F_SMALL, 76)
        tw = text_w(draw, txt, F_SMALL)
        draw.rectangle((W - tw - 6, H - 11, W - 1, H - 1), fill=(0, 0, 0))
        draw.text((W - tw - 3, H - 10), txt, font=F_SMALL,
                  fill=status_color(st["status"]))
    if last_ok == 0 or now - last_ok > STALE_S:
        draw.rectangle((0, H - 10, 44, H - 1), fill=(0, 0, 0))
        draw.text((2, H - 10), "STALE", font=F_SMALL, fill=YELLOW)


def compose(entry, idx, total, st, scroll, now):
    """Render one rotation page plus overlays into a full frame."""
    img, draw = new_frame()
    kind, arg = entry
    if kind == "listeners":
        page_listeners(draw, st)
    elif kind == "youtube":
        page_youtube(draw, st)
    elif kind == "fb_total":
        deltas = [p.get("delta") for p in st["fb_pages"]
                  if isinstance(p.get("delta"), (int, float))]
        page_fb(draw, "TOTAL", st["followers"],
                sum(deltas) if deltas else None)
    elif kind == "fb_group":
        page_fb_group(draw, arg)
    elif kind == "nws":
        page_nws(draw, arg, now)
    elif kind == "last_tone":
        page_last_tone(draw, now)
    elif kind == "clock":
        page_clock(draw)
    draw_overlays(draw, st, idx, total, now)
    return img


def slide(matrix, canvas, old_img, new_img):
    """~0.25s horizontal slide from old page to new page."""
    steps = 8
    for i in range(1, steps + 1):
        off = W * i // steps
        frame = Image.new("RGB", (W, H))
        frame.paste(old_img, (-off, 0))
        frame.paste(new_img, (W - off, 0))
        canvas.SetImage(frame)
        canvas = matrix.SwapOnVSync(canvas)
        time.sleep(0.028)
    return canvas


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
    alert_scroll_px = mcfg.get("scroll_px", 3)   # alert marquee speed
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
                        marquee_pass_seconds(current_alert.get("secondary"),
                                             F_MED, alert_scroll_px) + 1,
                        marquee_pass_seconds(current_alert.get("primary"),
                                             font(16), alert_scroll_px) + 1)
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
                scroll += alert_scroll_px
                canvas.SetImage(img)
                canvas = matrix.SwapOnVSync(canvas)
                time.sleep(0.05)
                continue
            current_alert = None                   # done — resume rotation
            page_since = now
            scroll = 0
            last_bright_check = 0.0                # re-evaluate night dim

        # ---- normal rotation ----
        with lock:
            st = dict(state)
        entries = page_list(st)
        if CFG.get("night_clock_only") and is_night():
            entries = [("clock", None)]
        total = len(entries)
        if page >= total:
            page = 0

        if now - last_bright_check > 60:
            last_bright_check = now
            matrix.brightness = (CFG.get("brightness_night", 20) if is_night()
                                 else CFG.get("brightness_day", 60))

        if total > 1 and now - page_since >= CFG.get("rotate_seconds", 8):
            old_img = compose(entries[page], page, total, st, scroll, now)
            page = (page + 1) % total
            page_since = now
            scroll = 0
            new_img = compose(entries[page], page, total, st, scroll, now)
            canvas = slide(matrix, canvas, old_img, new_img)
            continue

        img = compose(entries[page], page, total, st, scroll, now)
        canvas.SetImage(img)
        canvas = matrix.SwapOnVSync(canvas)
        time.sleep(0.2)


if __name__ == "__main__":
    main()
