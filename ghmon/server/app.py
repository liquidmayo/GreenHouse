"""Flask app for the master dashboard: ingest API + state API + static UI.

If dashboard_password is set, the UI and read APIs require a login session;
/api/ingest stays open to agents (protected by the shared API key) so remote
reporting is unaffected.
"""
import logging
import os
import secrets

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


def create_app(store, api_key, brand="SYSTEM MONITOR", dashboard_password=""):
    app = Flask("ghmon", static_folder=None)
    sessions = set()

    def authed():
        return request.cookies.get(SESSION_COOKIE) in sessions

    @app.before_request
    def gate():
        if not dashboard_password:
            return None
        # these carry their own API-key auth
        if request.path in ("/api/ingest", "/api/ticker",
                            "/api/webhook/sdrtrunk", "/login"):
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

    @app.post("/api/webhook/sdrtrunk")
    def sdrtrunk_webhook():
        """Per-call push from SDRTrunk's Local Webhook (JSON) broadcaster.
        The stream's Authorization field should be set to the shared api_key."""
        supplied = (request.headers.get("Authorization")
                    or request.headers.get("X-API-Key")
                    or request.args.get("key") or "")
        if not secrets.compare_digest(supplied, api_key):
            return jsonify({"ok": False, "error": "bad authorization"}), 401
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "bad payload"}), 400
        store.note_call(payload)
        return jsonify({"ok": True})

    @app.get("/api/ticker")
    def ticker():
        """Compact listener-count feed for embedded displays (T-Display etc.)."""
        supplied = request.headers.get("X-API-Key") or request.args.get("key") or ""
        if not secrets.compare_digest(supplied, api_key):
            return jsonify({"ok": False, "error": "bad api key"}), 401
        data = store.state()
        rdio = None
        thinline = None
        worst = "ok"
        sev = {"ok": 0, "unknown": 0, "warn": 1, "crit": 2}
        for machine in data.get("machines", {}).values():
            for comp in machine.get("components", []):
                metrics = comp.get("metrics", {})
                if isinstance(metrics.get("listeners"), (int, float)):
                    rdio = (rdio or 0) + metrics["listeners"]
                if isinstance(metrics.get("listener_count"), (int, float)):
                    thinline = (thinline or 0) + metrics["listener_count"]
                if sev.get(comp.get("status"), 0) > sev[worst]:
                    worst = comp.get("status")
        stats = store.call_stats()
        last = stats["recent"][0] if stats["recent"] else None
        last_call = None
        if last:
            last_call = {"talkgroup": last["talkgroup"], "system": last["system"],
                         "age_s": round(data["ts"] - last["ts"])}
        return jsonify({"rdio": rdio, "thinline": thinline,
                        "status": worst, "ts": data["ts"],
                        "calls_min": stats["last_min"], "last_call": last_call})

    @app.get("/api/state")
    def state():
        data = store.state()
        data["brand"] = brand
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
