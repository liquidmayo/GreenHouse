"""Collector agent: runs probes on a cycle and delivers payloads to a sink.

The same agent runs on the master machine (LocalSink, direct into the store)
and on companion machines (HttpSink, POST to the master's ingest API).
"""
import collections
import logging
import threading
import time

import psutil
import requests

from . import __version__
from .probes import build_probe
from .probes.base import worst

log = logging.getLogger("ghmon.agent")


class Component:
    def __init__(self, cfg):
        self.id = cfg["id"]
        self.label = cfg.get("label", cfg["id"])
        self.featured = cfg.get("featured", [])  # metric keys to spotlight in the UI
        self.probes = [build_probe(p) for p in cfg.get("probes", [])]

    def collect(self):
        metrics = {}
        events = []
        statuses = []
        summaries = []
        for probe in self.probes:
            res = probe.safe_run()
            statuses.append(res["status"])
            metrics.update(res["metrics"])
            events.extend(res["events"])
            if res["summary"] and res["status"] != "ok":
                summaries.append(res["summary"])
        status = worst(statuses) if statuses else "unknown"
        summary = "; ".join(summaries) if summaries else (
            "All checks passing" if status == "ok" else "")
        return {
            "id": self.id,
            "label": self.label,
            "status": status,
            "summary": summary,
            "featured": self.featured,
            "metrics": metrics,
            "events": events,
        }


def system_snapshot(cfg):
    out = {"status": "ok", "notes": []}
    try:
        out["cpu_pct"] = psutil.cpu_percent(interval=0.4)
        mem = psutil.virtual_memory()
        out["mem_pct"] = mem.percent
        out["mem_used_gb"] = round(mem.used / (1024 ** 3), 1)
        out["mem_total_gb"] = round(mem.total / (1024 ** 3), 1)
        disks = []
        for path in cfg.get("disks", []):
            try:
                usage = psutil.disk_usage(path)
                disk = {
                    "mount": path,
                    "used_pct": usage.percent,
                    "free_gb": round(usage.free / (1024 ** 3), 1),
                }
                if usage.percent >= cfg.get("disk_crit_pct", 95):
                    out["status"] = "crit"
                    out["notes"].append(f"disk {path} at {usage.percent}%")
                elif usage.percent >= cfg.get("disk_warn_pct", 85):
                    if out["status"] == "ok":
                        out["status"] = "warn"
                    out["notes"].append(f"disk {path} at {usage.percent}%")
                disks.append(disk)
            except OSError:
                disks.append({"mount": path, "error": "unavailable"})
        out["disks"] = disks
        if out["cpu_pct"] >= cfg.get("cpu_warn_pct", 92):
            if out["status"] == "ok":
                out["status"] = "warn"
            out["notes"].append(f"CPU at {out['cpu_pct']}%")
        if out["mem_pct"] >= cfg.get("mem_warn_pct", 92):
            if out["status"] == "ok":
                out["status"] = "warn"
            out["notes"].append(f"memory at {out['mem_pct']}%")
    except Exception as exc:
        out["status"] = "unknown"
        out["notes"].append(f"system stats error: {exc}")
    return out


class HttpSink:
    """Delivers payloads to a remote master, buffering while it is unreachable."""

    def __init__(self, master_url, api_key, buffer_size=40):
        self.url = master_url.rstrip("/") + "/api/ingest"
        self.api_key = api_key
        self.buffer = collections.deque(maxlen=buffer_size)

    def send(self, payload):
        self.buffer.append(payload)
        flushed = 0
        while self.buffer and flushed < 5:
            item = self.buffer[0]
            try:
                resp = requests.post(self.url, json=item,
                                     headers={"X-API-Key": self.api_key}, timeout=8)
                if resp.status_code == 401:
                    log.error("master rejected API key — check api_key in monitors.yml")
                    return
                resp.raise_for_status()
                self.buffer.popleft()
                flushed += 1
            except Exception as exc:
                log.warning("master unreachable (%s); buffering %d payloads",
                            exc.__class__.__name__, len(self.buffer))
                return


class LocalSink:
    """Master mode: hand payloads straight to the in-process store."""

    def __init__(self, store):
        self.store = store

    def send(self, payload):
        self.store.ingest(payload)


class Agent:
    def __init__(self, cfg, sink):
        self.cfg = cfg
        self.sink = sink
        self.components = [Component(c) for c in cfg.get("components", [])]
        self.interval = cfg.get("interval", 15)
        self._stop = threading.Event()

    def cycle(self):
        payload = {
            "machine": self.cfg["machine"],
            "agent_version": __version__,
            "ts": time.time(),
            "interval": self.interval,
            "system": system_snapshot(self.cfg.get("system", {})),
            "components": [c.collect() for c in self.components],
        }
        self.sink.send(payload)
        return payload

    def run_forever(self):
        log.info("agent started: machine=%s components=%d interval=%ss",
                 self.cfg["machine"], len(self.components), self.interval)
        while not self._stop.is_set():
            start = time.time()
            try:
                self.cycle()
            except Exception:
                log.exception("collection cycle failed")
            elapsed = time.time() - start
            self._stop.wait(max(1.0, self.interval - elapsed))

    def start_background(self):
        thread = threading.Thread(target=self.run_forever, name="gh-agent", daemon=True)
        thread.start()
        return thread

    def stop(self):
        self._stop.set()
