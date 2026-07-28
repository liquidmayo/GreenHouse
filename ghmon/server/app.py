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
        if request.path in ("/api/ingest", "/login"):
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
