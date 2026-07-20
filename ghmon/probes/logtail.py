r"""Log tail probe: incrementally read a log file, classify new lines
against regex patterns, and track a heartbeat pattern's silence.

Handles rotation (file shrinks -> start over) and caps how much is read
per cycle so a flood can't stall the agent.

Config:
  type: logtail
  path: C:\Users\micha\SDRTrunk\logs\sdrtrunk_app.log
  encoding: utf-8
  max_bytes_per_cycle: 524288
  patterns:
    - regex: "No Tuner Available"
      level: warn
      label: "Tuner exhausted"
    - regex: "Rdio Scanner API file upload fail"
      level: warn
      label: "Stream upload failing"
  heartbeat:
    regex: "Scanner feed: fetched"
    max_silence: 300
    level: warn
    label: "Heartbeat silent"
  level_if_missing: warn     # status when the log file doesn't exist
"""
import os
import re
import time

from .base import Probe, result, event

MAX_EVENTS_PER_CYCLE = 12


class LogTailProbe(Probe):
    def __init__(self, cfg):
        super().__init__(cfg)
        self._pos = None
        self._last_heartbeat = None
        self._patterns = [
            (re.compile(p["regex"]), p.get("level", "warn"), p.get("label", p["regex"]))
            for p in cfg.get("patterns", [])
        ]
        hb = cfg.get("heartbeat")
        self._hb = None
        if hb:
            self._hb = (re.compile(hb["regex"]), hb.get("max_silence", 600),
                        hb.get("level", "warn"), hb.get("label", "Heartbeat silent"))

    def run(self):
        cfg = self.cfg
        path = cfg["path"]
        if not os.path.exists(path):
            level = cfg.get("level_if_missing", "warn")
            self._pos = None
            return result(level, "log file missing",
                          events=[event(level, "Log missing", path)])

        stat = os.stat(path)
        size = stat.st_size
        now = time.time()

        first_pass = self._pos is None
        if first_pass:
            # Start at the end; seed the heartbeat clock from file mtime so a
            # freshly-started agent doesn't instantly alarm on an active log.
            self._pos = size
            if self._hb:
                self._last_heartbeat = stat.st_mtime
        elif size < self._pos:
            self._pos = 0  # rotated/truncated

        events = []
        status = "ok"
        summaries = []
        counts = {}

        if size > self._pos:
            budget = cfg.get("max_bytes_per_cycle", 512 * 1024)
            start = max(self._pos, size - budget) if (size - self._pos) > budget else self._pos
            with open(path, "r", encoding=cfg.get("encoding", "utf-8"), errors="replace") as fh:
                fh.seek(start)
                chunk = fh.read(size - start)
            self._pos = size
            for line in chunk.splitlines():
                if self._hb and self._hb[0].search(line):
                    self._last_heartbeat = now
                for regex, level, label in self._patterns:
                    if regex.search(line):
                        counts[label] = counts.get(label, 0) + 1
                        if len(events) < MAX_EVENTS_PER_CYCLE:
                            events.append(event(level, label, line.strip()))
                        if _sev(level) > _sev(status):
                            status = level
                        break

        for label, n in counts.items():
            summaries.append(f"{label} x{n}" if n > 1 else label)

        metrics = {"log_size_mb": round(size / (1024 * 1024), 1),
                   "log_age_s": round(now - stat.st_mtime)}

        if self._hb:
            regex, max_silence, level, label = self._hb
            if self._last_heartbeat is not None:
                silence = round(now - self._last_heartbeat)
                metrics["heartbeat_silence_s"] = silence
                if silence > max_silence:
                    summaries.append(f"{label} ({silence}s)")
                    events.append(event(level, label, f"no heartbeat match in {silence}s"))
                    if _sev(level) > _sev(status):
                        status = level

        return result(status, "; ".join(summaries), metrics, events)


def _sev(s):
    return {"ok": 0, "unknown": 0, "warn": 1, "crit": 2}.get(s, 0)
