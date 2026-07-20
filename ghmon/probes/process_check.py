"""Process probe: is a process running, and how much memory is it using.

Config:
  type: process
  match: thinline-radio.exe     # substring match on process name (case-insensitive)
  cmdline_contains: dsheirer    # optional additional substring match on command line
  level_if_down: crit
"""
import psutil

from .base import Probe, result, event


class ProcessProbe(Probe):
    def run(self):
        cfg = self.cfg
        name_sub = (cfg.get("match") or "").lower()
        cmd_sub = (cfg.get("cmdline_contains") or "").lower()
        found = []
        for proc in psutil.process_iter(["name", "cmdline", "memory_info"]):
            try:
                pname = (proc.info.get("name") or "").lower()
                if name_sub and name_sub not in pname:
                    continue
                if cmd_sub:
                    cmdline = " ".join(proc.info.get("cmdline") or []).lower()
                    if cmd_sub not in cmdline:
                        continue
                found.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        if not found:
            level = cfg.get("level_if_down", "crit")
            label = cfg.get("match") or cfg.get("cmdline_contains")
            return result(level, "process not running",
                          events=[event(level, "Process down", f"no process matching {label!r}")])

        mem_mb = 0
        for proc in found:
            try:
                mem_mb += proc.info["memory_info"].rss / (1024 * 1024)
            except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError, TypeError):
                pass
        return result("ok", "", {"proc_mem_mb": round(mem_mb), "proc_count": len(found)})
