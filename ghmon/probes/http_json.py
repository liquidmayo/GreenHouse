"""HTTP JSON probe: poll an endpoint, apply checks against the response,
collect metrics, and compute per-minute rates for monotonic counters.

Config:
  type: http_json
  url: http://127.0.0.1:3001/api/status/performance
  method: GET            # or POST
  json: {...}            # optional POST body
  headers: {...}         # optional
  basic_auth: [user, pw] # optional
  timeout: 6
  level_if_down: crit    # status when the request itself fails
  collect: [total_calls, memory_stats.alloc_mb]   # dotted paths -> metrics
  rates: [total_calls]   # counters -> <name>_per_min metric
  checks:
    - path: db_ok
      equals: true
      level: crit
      label: "Database down"
    - path: cameras.updated       # epoch-seconds field
      max_age: 420
      level: warn
      label: "Camera feed stale"
    - path: transcription_queue_depth
      max: 50
      level: warn
      label: "Transcription backlog"
    - path: calls_last_minute
      min: 1
      level: warn
      label: "No calls in the last minute"
"""
import time

import requests

from .base import Probe, result, event


def dig(obj, path):
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


class HttpJsonProbe(Probe):
    def __init__(self, cfg):
        super().__init__(cfg)
        self._prev = {}  # counter name -> (ts, value)

    def run(self):
        cfg = self.cfg
        method = (cfg.get("method") or "GET").upper()
        timeout = cfg.get("timeout", 6)
        auth = tuple(cfg["basic_auth"]) if cfg.get("basic_auth") else None
        try:
            resp = requests.request(
                method,
                cfg["url"],
                json=cfg.get("json"),
                headers=cfg.get("headers"),
                auth=auth,
                timeout=timeout,
            )
        except Exception as exc:
            level = cfg.get("level_if_down", "crit")
            return result(level, f"unreachable: {exc.__class__.__name__}",
                          events=[event(level, "Endpoint unreachable", f"{cfg['url']}: {exc}")])

        if resp.status_code >= 400:
            level = cfg.get("level_if_down", "crit")
            return result(level, f"HTTP {resp.status_code} from {cfg['url']}",
                          events=[event(level, f"HTTP {resp.status_code}", cfg["url"])])

        try:
            data = resp.json()
        except ValueError:
            return result("warn", "response was not JSON")

        metrics = {"latency_ms": round(resp.elapsed.total_seconds() * 1000)}
        events = []
        status = "ok"
        now = time.time()

        collect = cfg.get("collect", [])
        last_parts = [str(p).split(".")[-1] for p in collect]
        for path in collect:
            val = dig(data, path)
            if val is not None and not isinstance(val, (dict, list)):
                key = str(path).split(".")[-1]
                if last_parts.count(key) > 1:  # ambiguous — keep the full path
                    key = str(path).replace(".", "_")
                metrics[key] = val

        for name in cfg.get("rates", []):
            val = dig(data, name)
            if isinstance(val, (int, float)):
                prev = self._prev.get(name)
                if prev and now > prev[0]:
                    delta = val - prev[1]
                    if delta >= 0:
                        metrics[f"{name.split('.')[-1]}_per_min"] = round(
                            delta / ((now - prev[0]) / 60.0), 1)
                self._prev[name] = (now, val)

        summaries = []
        for check in cfg.get("checks", []):
            val = dig(data, check.get("path"))
            level = check.get("level", "warn")
            label = check.get("label", f"check failed: {check.get('path')}")
            failed = False
            detail = ""
            if val is None:
                # Missing field: treat as informational, endpoint may be older version
                continue
            if "equals" in check and val != check["equals"]:
                failed, detail = True, f"{check['path']}={val!r}"
            if "min" in check and isinstance(val, (int, float)) and val < check["min"]:
                failed, detail = True, f"{check['path']}={val} < {check['min']}"
            if "max" in check and isinstance(val, (int, float)) and val > check["max"]:
                failed, detail = True, f"{check['path']}={val} > {check['max']}"
            if "max_age" in check and isinstance(val, (int, float)):
                age = now - val
                metrics[f"{str(check['path']).split('.')[0]}_age_s"] = round(age)
                if age > check["max_age"]:
                    failed, detail = True, f"{check['path']} is {round(age)}s old"
            if failed:
                summaries.append(label)
                events.append(event(level, label, detail))
                if _sev(level) > _sev(status):
                    status = level

        # Optional: surface a JSON alerts array (e.g. SDRTrunk /health "alerts")
        # as monitor events. Config:
        #   alert_events: {path: alerts, level_key: level, msg_key: msg,
        #                  label: "SDRTrunk alert", crit_on_error: false}
        ae = cfg.get("alert_events")
        if ae:
            entries = dig(data, ae.get("path", "alerts"))
            if isinstance(entries, list) and entries:
                label = ae.get("label", "Alert")
                for entry in entries[:10]:
                    if isinstance(entry, dict):
                        raw = str(entry.get(ae.get("level_key", "level"), "warn")).lower()
                        msg = str(entry.get(ae.get("msg_key", "msg"), entry))
                    else:
                        raw, msg = "warn", str(entry)
                    lvl = "crit" if raw in ("crit", "critical") or (
                        raw == "error" and ae.get("crit_on_error")) else "warn"
                    events.append(event(lvl, label, msg))
                    if _sev(lvl) > _sev(status):
                        status = lvl
                summaries.append(f"{len(entries)} active alert(s)")

        return result(status, "; ".join(summaries), metrics, events)


def _sev(s):
    return {"ok": 0, "unknown": 0, "warn": 1, "crit": 2}.get(s, 0)
