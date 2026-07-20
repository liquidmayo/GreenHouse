"""File activity probe: watch a file (or newest file matching a glob) and
alert when it stops being written. A quiet database or event log usually
means the pipeline in front of it has died.

Config:
  type: file_activity
  path: D:\\rdio-scanner-windows-amd64-v6.6.3\\rdio-scanner.db
  # or a glob:  path: C:\\Users\\micha\\SDRTrunk\\event_logs\\*_call_events.log
  max_age: 900          # seconds since last modification before alerting
  level: warn
  label: "No recent call activity"
"""
import glob
import os
import time

from .base import Probe, result, event


class FileActivityProbe(Probe):
    def run(self):
        cfg = self.cfg
        path = cfg["path"]
        level = cfg.get("level", "warn")

        target = path
        if any(ch in path for ch in "*?["):
            matches = glob.glob(path)
            if not matches:
                return result(level, "no files match pattern",
                              events=[event(level, "Files missing", path)])
            target = max(matches, key=lambda p: os.path.getmtime(p))

        if not os.path.exists(target):
            return result(level, "file missing",
                          events=[event(level, "File missing", target)])

        stat = os.stat(target)
        age = round(time.time() - stat.st_mtime)
        metrics = {
            "file_age_s": age,
            "file_size_mb": round(stat.st_size / (1024 * 1024), 1),
        }
        max_age = cfg.get("max_age")
        if max_age and age > max_age:
            label = cfg.get("label", "File inactive")
            return result(level, f"{label} ({age}s)", metrics,
                          [event(level, label, f"{os.path.basename(target)} last written {age}s ago")])
        return result("ok", "", metrics)
