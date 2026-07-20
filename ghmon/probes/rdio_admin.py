"""rdio-scanner admin API probe.

rdio-scanner keeps its logs in its database (there is no log file), reachable
via the admin API: login with the admin password to get a JWT, then search
recent logs for call-ingest and listener activity.

If no password is configured the probe reports 'unknown' and stays dormant —
the component can still be covered by port/process/file_activity probes.

Config:
  type: rdio_admin
  url: http://127.0.0.1:3000
  password: ""              # admin password; blank disables the probe
  max_call_silence: 900     # warn if no successful 'newcall' in this window
"""
import re
import time
from datetime import datetime

import requests

from .base import Probe, result, event

LISTENERS_RE = re.compile(r"listeners count is (\d+)")


def entry_ts_ms(entry):
    """Best-effort epoch-ms from a log entry; rdio-scanner versions differ in
    field name and scale. Returns None if no usable timestamp."""
    for key in ("timestamp", "dateTime", "date", "createdAt", "ts"):
        val = entry.get(key)
        if val is None:
            continue
        if isinstance(val, (int, float)) and val > 0:
            return float(val) if val > 1e12 else float(val) * 1000
        if isinstance(val, str):
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp() * 1000
            except ValueError:
                continue
    return None


class RdioAdminProbe(Probe):
    def __init__(self, cfg):
        super().__init__(cfg)
        self._token = None

    def _login(self, base):
        resp = requests.post(f"{base}/api/admin/login",
                             json={"password": self.cfg["password"]}, timeout=6)
        resp.raise_for_status()
        data = resp.json()
        token = data.get("token") if isinstance(data, dict) else data
        if not token or not isinstance(token, str):
            raise RuntimeError("login gave no token")
        self._token = token

    def _fetch_logs(self, base):
        return requests.post(
            f"{base}/api/admin/logs",
            json={"limit": 200, "offset": 0, "sort": -1},
            headers={"Authorization": self._token},
            timeout=8,
        )

    def run(self):
        cfg = self.cfg
        if not cfg.get("password"):
            return result("unknown", "admin password not configured (log analysis disabled)")

        base = cfg["url"].rstrip("/")
        try:
            if not self._token:
                self._login(base)
            resp = self._fetch_logs(base)
            if resp.status_code in (401, 403):
                self._login(base)  # token expired — one retry
                resp = self._fetch_logs(base)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            self._token = None
            return result("warn", f"admin API error: {exc.__class__.__name__}",
                          events=[event("warn", "rdio admin API error", str(exc))])

        logs = data.get("logs") or data.get("results") or []
        now_ms = time.time() * 1000
        newest_call_ms = None
        api_errors = 0
        listeners = None
        for entry in logs:
            msg = str(entry.get("message", ""))
            if "newcall" in msg and "success" in msg:
                ts = entry_ts_ms(entry)
                if ts is not None and (newest_call_ms is None or ts > newest_call_ms):
                    newest_call_ms = ts
            elif msg.startswith("api:"):
                api_errors += 1
            elif listeners is None:
                m = LISTENERS_RE.search(msg)
                if m:
                    listeners = int(m.group(1))

        metrics = {}
        events_out = []
        status = "ok"
        summaries = []
        if listeners is not None:
            metrics["listeners"] = listeners
        if api_errors:
            metrics["api_errors_recent"] = api_errors
            summaries.append(f"{api_errors} ingest errors in recent logs")
            status = "warn"
            events_out.append(event("warn", "Ingest errors", f"{api_errors} 'api:' errors in last 200 log entries"))
        if newest_call_ms is not None:
            silence = round((now_ms - newest_call_ms) / 1000)
            metrics["last_call_age_s"] = silence
            if silence > cfg.get("max_call_silence", 900):
                summaries.append(f"no calls ingested for {silence}s")
                status = "warn"
                events_out.append(event("warn", "Call ingest silent", f"last successful newcall {silence}s ago"))

        return result(status, "; ".join(summaries), metrics, events_out)
