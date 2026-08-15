"""GreenHouse Monitor entrypoint.

Master mode (dashboard + local collector):
    python -m ghmon.main --config monitors.yml

Companion agent mode (collect here, send to a remote master):
    python -m ghmon.main --config monitors.yml     (with mode: agent in the yml)
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ghmon.agent import Agent, HttpSink, LocalSink
from ghmon.config import load_config


def main():
    parser = argparse.ArgumentParser(description="Infrastructure monitor")
    parser.add_argument("--config", default="monitors.yml", help="path to monitors.yml")
    parser.add_argument("--mode", choices=["master", "agent"],
                        help="override mode from config")
    parser.add_argument("--log", default=None,
                        help="also write log output to this file (rotating, 2 MB x 3); "
                             "required for useful diagnostics when run windowless via pythonw")
    args = parser.parse_args()

    handlers = []
    if sys.stderr is not None:  # pythonw has no console streams
        handlers.append(logging.StreamHandler())
    if args.log:
        from logging.handlers import RotatingFileHandler
        os.makedirs(os.path.dirname(os.path.abspath(args.log)), exist_ok=True)
        handlers.append(RotatingFileHandler(args.log, maxBytes=2_000_000,
                                            backupCount=3, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=handlers or [logging.NullHandler()])
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    log = logging.getLogger("ghmon")

    cfg = load_config(args.config)
    mode = args.mode or cfg.get("mode", "agent")

    if mode == "master":
        from ghmon.server.app import create_app
        from ghmon.server.store import Store

        base_dir = os.path.dirname(os.path.abspath(args.config))
        store = Store(os.path.join(base_dir, "data", "monitor.db"))
        agent = Agent(cfg, LocalSink(store))
        agent.start_background()

        server_cfg = cfg["server"]
        app = create_app(store, cfg["api_key"], cfg.get("brand", "SYSTEM MONITOR"),
                         cfg.get("dashboard_password", ""))
        log.info("GreenHouse Monitor master dashboard at http://%s:%s/",
                 server_cfg["host"], server_cfg["port"])
        app.run(host=server_cfg["host"], port=server_cfg["port"],
                debug=False, threaded=True, use_reloader=False)
    else:
        log.info("companion agent mode -> %s", cfg["master_url"])
        agent = Agent(cfg, HttpSink(cfg["master_url"], cfg["api_key"]))
        agent.run_forever()


if __name__ == "__main__":
    main()
