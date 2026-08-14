"""Configuration loading for GreenHouse Monitor."""
import os

import yaml

DEFAULTS = {
    "brand": "SYSTEM MONITOR",  # dashboard title/footer text
    "machine": None,          # defaults to hostname
    "mode": "agent",          # master | agent
    "interval": 15,           # seconds between collection cycles
    "master_url": "http://localhost:8090",
    "api_key": "greenhouse-change-me",
    "dashboard_password": "",   # blank = open dashboard; set to require login
    "server": {
        "host": "0.0.0.0",
        "port": 8090,
    },
    "system": {
        "disks": [],
        "cpu_warn_pct": 92,
        "mem_warn_pct": 92,
        "disk_warn_pct": 85,
        "disk_crit_pct": 95,
    },
    "components": [],
}


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        try:
            raw = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            hint = ""
            if "unknown escape character" in str(exc):
                hint = ("\nHINT: a Windows path inside DOUBLE quotes treats \\ as an "
                        "escape. Use single quotes for paths: path: 'C:\\my\\file.log'")
            raise SystemExit(f"Config error in {path}:\n{exc}{hint}")
    cfg = dict(DEFAULTS)
    cfg.update(raw)
    for key in ("server", "system"):
        merged = dict(DEFAULTS[key])
        merged.update(raw.get(key) or {})
        cfg[key] = merged
    if not cfg.get("machine"):
        cfg["machine"] = os.environ.get("COMPUTERNAME") or os.uname().nodename
    return cfg
