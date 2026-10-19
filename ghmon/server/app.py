"""Flask app for the master dashboard: ingest API + state API + static UI.

If dashboard_password is set, the UI and read APIs require a login session;
/api/ingest stays open to agents (protected by the shared API key) so remote
reporting is unaffected.
"""
import logging
import os
import secrets
import time

from flask import Flask, jsonify, make_response, redirect, request, send_from_directory

log = logging.getLogger("ghmon.server")

# app.py lives in <root>/ghmon/server; the UI lives in <root>/ui
UI_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "ui")

SESSION_COOKIE = "ghmon_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 days

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Monitor Login</title>
<style>
  body { background: #050805; color: #fff; font-family: "Segoe UI", system-ui, sans-serif;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
  form { background: #0c120d; border: 1px solid #1f3324; border-radius: 10px;
         padding: 28px 32px; text-align: center; box-shadow: 0 8px 24px rgba(0,0,0,.6); }
  h1 { font-size: 14px; letter-spacing: 2px; color: #22c55e; margin: 0 0 16px; }
  input { background: #050805; border: 1px solid #1f3324; border-radius: 6px;
          color: #fff; padding: 9px 12px; font-size: 14px; width: 220px; }
  input:focus { outline: none; border-color: #22c55e; }
  button { margin-top: 12px; width: 100%; background: #166534; color: #fff; border: none;
           border-radius: 6px; padding: 9px; font-size: 13px; letter-spacing: 1px; cursor: pointer; }
  button:hover { background: #22c55e; color: #050805; }
  .err { color: #ef4444; font-size: 12px; margin-top: 10px; min-height: 14px; }
</style></head><body>
<form method="post">
  <h1>MONITOR ACCESS</h1>
  <input type="password" name="password" placeholder="Password" autofocus>
  <button type="submit">SIGN IN</button>
  <div class="err">{{error}}</div>
</form></body></html>"""


def create_app(store, api_key, brand="SYSTEM MONITOR", dashboard_password="",
               ui_cfg=None, stale_cfg=None):
    app = Flask("ghmon", static_folder=None)
    ui_cfg = ui_cfg or {}
    stale_cfg = stale_cfg or []
    sessions = set()
    _stale_cache = {"ts": 0.0, "flags": {}}

    def stale_flags():
        """(machine, component) -> note, for watched metrics whose value has
        been flat for the rule's whole window — a silently stale upstream
        source (e.g. a scraper serving the same cached count for days).
        Evaluated from the trends DB so restarts don't reset the clock."""
        import fnmatch
        now = time.time()
        if now - _stale_cache["ts"] < 600:
            return _stale_cache["flags"]
        flags = {}
        data = {}
        try:
            data = store.state()
            for rule in stale_cfg:
                metric = rule.get("metric", "")
                days = float(rule.get("days", 5))
                for mname, machine in data.get("machines", {}).items():
                    for comp in machine.get("components", []):
                        cid = comp.get("id", "")
                        if not fnmatch.fnmatch(cid, rule.get("component", "*")):
                            continue
                        pts = store.trend(mname, cid, metric, days * 24)
                        # need most of the window on record before judging
                        if len(pts) < days * 24 * 12 * 0.6:
                            continue
                        vals = {p["avg"] for p in pts}
                        if len(vals) == 1:
                            flags[(mname, cid)] = (
                                f"{metric} unchanged for {days:g}+ days — "
                                "source likely stale")
        except Exception:
            log.exception("stale check failed")
        # don't cache a cold-start evaluation: right after launch the first
        # ticker poll can land before the local agent's first ingest, and an
        # empty machine list must not pin "no flags" for the cache window
        if data.get("machines"):
            _stale_cache["ts"] = now
            _stale_cache["flags"] = flags
        return flags

    def apply_stale(data):
        for (mname, cid), note in stale_flags().items():
            for comp in data.get("machines", {}).get(mname, {}) \
                            .get("components", []):
                if comp.get("id") == cid:
                    if comp.get("status") in ("ok", "unknown"):
                        comp["status"] = "warn"
                    comp["summary"] = (comp.get("summary", "") +
                                       " | " + note).strip(" |")
        return data

    def authed():
        return request.cookies.get(SESSION_COOKIE) in sessions

    @app.before_request
    def gate():
        if not dashboard_password:
            return None
        # these carry their own API-key auth
        if request.path in ("/api/ingest", "/api/ticker", "/login"):
            return None
        if authed():
            return None
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "login required"}), 401
        return redirect("/login")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not dashboard_password:
            return redirect("/")
        error = ""
        if request.method == "POST":
            supplied = request.form.get("password") or ""
            if secrets.compare_digest(supplied, dashboard_password):
                token = secrets.token_urlsafe(32)
                sessions.add(token)
                resp = make_response(redirect("/"))
                resp.set_cookie(SESSION_COOKIE, token, httponly=True,
                                samesite="Lax", max_age=SESSION_MAX_AGE)
                return resp
            error = "Incorrect password."
            log.warning("failed dashboard login from %s", request.remote_addr)
        return LOGIN_PAGE.replace("{{error}}", error)

    @app.post("/api/ingest")
    def ingest():
        if request.headers.get("X-API-Key") != api_key:
            return jsonify({"ok": False, "error": "bad api key"}), 401
        payload = request.get_json(silent=True)
        if not payload or "machine" not in payload:
            return jsonify({"ok": False, "error": "bad payload"}), 400
        store.ingest(payload)
        return jsonify({"ok": True})

    @app.get("/api/ticker")
    def ticker():
        """Compact listener-count feed for embedded displays (T-Display etc.)."""
        supplied = request.headers.get("X-API-Key") or request.args.get("key") or ""
        if not secrets.compare_digest(supplied, api_key):
            return jsonify({"ok": False, "error": "bad api key"}), 401
        data = apply_stale(store.state())
        rdio = None
        thinline = None
        followers = None
        followers_pages = []
        viewers = None
        nws = []
        worst = "ok"
        worst_label = None
        sev = {"ok": 0, "unknown": 0, "warn": 1, "crit": 2}

        def since_midnight_delta(mname, cid, metric, current):
            """Metric gain since local midnight, from the 5-min trend rollups."""
            try:
                lt = time.localtime()
                hours = lt.tm_hour + lt.tm_min / 60 + 0.1
                pts = store.trend(mname, cid, metric, hours)
                if pts:
                    return int(round(current - pts[0]["avg"]))
            except Exception:
                log.exception("ticker delta failed")
            return None

        for mname, machine in data.get("machines", {}).items():
            for comp in machine.get("components", []):
                metrics = comp.get("metrics", {})
                if isinstance(metrics.get("listeners"), (int, float)):
                    rdio = (rdio or 0) + metrics["listeners"]
                if isinstance(metrics.get("listener_count"), (int, float)):
                    thinline = (thinline or 0) + metrics["listener_count"]
                if isinstance(metrics.get("followers"), (int, float)):
                    followers = (followers or 0) + metrics["followers"]
                    name = comp.get("label", comp.get("id", ""))
                    if name.upper().startswith("FB "):
                        name = name[3:]
                    followers_pages.append(
                        {"name": name, "count": int(metrics["followers"]),
                         "delta": since_midnight_delta(
                             mname, comp.get("id", ""), "followers",
                             metrics["followers"])})
                if isinstance(metrics.get("viewers"), (int, float)):
                    viewers = (viewers or 0) + metrics["viewers"]
                if isinstance(metrics.get("nws_list"), list):
                    nws = nws + metrics["nws_list"]
                if sev.get(comp.get("status"), 0) > sev[worst]:
                    worst = comp.get("status")
                    worst_label = comp.get("label", comp.get("id", ""))
        # compact listener-trend sparkline for embedded displays: ~24 points
        # of merged listener averages over the last 6 hours
        spark = None
        try:
            pts = store.trend_merged("listeners", 6)
            if pts:
                step = max(1, len(pts) // 24)
                spark = [round(p["avg"]) for p in pts[::step]][-24:]
        except Exception:
            log.exception("ticker spark failed")
        return jsonify({"rdio": rdio, "thinline": thinline,
                        "followers": followers, "viewers": viewers,
                        "followers_pages": followers_pages,
                        "nws": nws,
                        "status": worst,
                        "worst_label": (worst_label
                                        if worst in ("warn", "crit") else None),
                        "ts": data["ts"],
                        "spark": spark})

    @app.get("/api/trend")
    def trend():
        """5-minute rollup history of a spotlight metric.
        ?machine=X&component=Y&metric=Z&hours=24, or ?metric=Z&merged=1
        to sum the metric across every machine/component."""
        metric = request.args.get("metric", "")
        hours = min(float(request.args.get("hours", 24)), 24 * 30)
        if request.args.get("merged"):
            return jsonify(store.trend_merged(metric, hours))
        return jsonify(store.trend(request.args.get("machine", ""),
                                   request.args.get("component", ""), metric, hours))

    @app.get("/api/state")
    def state():
        data = apply_stale(store.state())
        data["brand"] = brand
        data["ui"] = ui_cfg
        return jsonify(data)

    @app.get("/api/history")
    def history():
        machine = request.args.get("machine", "")
        component = request.args.get("component", "")
        hours = min(float(request.args.get("hours", 6)), 48)
        return jsonify(store.history(machine, component, hours))

    @app.get("/api/events")
    def events():
        limit = int(request.args.get("limit", 100))
        return jsonify(store.events(limit))

    @app.get("/")
    def index():
        return send_from_directory(UI_DIR, "index.html")

    @app.get("/<path:filename>")
    def static_files(filename):
        return send_from_directory(UI_DIR, filename)

    return app
