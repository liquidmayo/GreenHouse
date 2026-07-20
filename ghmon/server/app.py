"""Flask app for the master dashboard: ingest API + state API + static UI."""
import logging
import os

from flask import Flask, jsonify, request, send_from_directory

log = logging.getLogger("ghmon.server")

# app.py lives in <root>/ghmon/server; the UI lives in <root>/ui
UI_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "ui")


def create_app(store, api_key, brand="SYSTEM MONITOR"):
    app = Flask("ghmon", static_folder=None)

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
