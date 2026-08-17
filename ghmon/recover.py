"""Restart-on-failure hooks: optionally run a command when a component has
been CRIT for a while. Opt-in per component, rate-limited, always logged.

Component config:
  on_crit:
    run: 'D:\\HGEyeProject\\start.bat'   # command to launch (via cmd, detached)
    after_seconds: 120                    # crit must persist this long first
    max_per_hour: 2                       # hard cap on restart attempts
    cooldown_seconds: 300                 # min gap between attempts
    hidden: true                          # launch with no console window

Design notes:
  - Deliberately conservative: nothing runs on WARN, and nothing runs until
    the crit state has persisted `after_seconds`. Suits services the operator
    owns (own Flask apps, node bots); do NOT hook SDRTrunk/rdio-scanner —
    those failures usually need a human and a restart loop can hide them.
  - Every attempt is emitted as a component event so it appears in the
    store (and, via the notifier at min_severity warn, in Discord).
"""
import collections
import logging
import os
import subprocess
import time

from .probes.base import event

log = logging.getLogger("ghmon.recover")


class Recovery:
    def __init__(self, cfg):
        self.cfg = cfg or {}
        self.enabled = bool(self.cfg.get("run"))
        self.after = float(self.cfg.get("after_seconds", 120))
        self.max_per_hour = int(self.cfg.get("max_per_hour", 2))
        self.cooldown = float(self.cfg.get("cooldown_seconds", 300))
        self.hidden = bool(self.cfg.get("hidden", True))
        self.crit_since = None
        self.attempts = collections.deque()   # timestamps of launches
        self.last_attempt = 0.0

    def observe(self, status, label):
        """Call once per collection cycle. Returns a list of events (possibly
        empty) describing any recovery action taken or refused."""
        if not self.enabled:
            return []
        now = time.time()
        if status != "crit":
            if self.crit_since is not None:
                self.crit_since = None
            return []
        if self.crit_since is None:
            self.crit_since = now
            return []
        if now - self.crit_since < self.after:
            return []
        if now - self.last_attempt < self.cooldown:
            return []
        # prune attempts older than an hour, enforce the cap
        while self.attempts and now - self.attempts[0] > 3600:
            self.attempts.popleft()
        if len(self.attempts) >= self.max_per_hour:
            # say so once per cooldown window, not every cycle
            self.last_attempt = now
            msg = (f"restart NOT attempted: {self.max_per_hour}/hour limit reached "
                   f"— {label} needs a human")
            log.warning(msg)
            return [event("warn", "Auto-restart limit reached", msg)]

        self.attempts.append(now)
        self.last_attempt = now
        # reset the timer so a successful restart isn't immediately re-hit;
        # if it stays crit, after_seconds must elapse again
        self.crit_since = now
        cmd = self.cfg["run"]
        try:
            flags = 0
            if os.name == "nt":
                flags = subprocess.CREATE_NEW_PROCESS_GROUP
                if self.hidden:
                    flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
                else:
                    flags |= getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            subprocess.Popen(cmd, shell=True, creationflags=flags,
                             cwd=os.path.dirname(cmd) if os.path.isabs(cmd) else None,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True)
            msg = f"launched: {cmd} (attempt {len(self.attempts)}/{self.max_per_hour} this hour)"
            log.warning("auto-restart %s -> %s", label, msg)
            return [event("warn", "Auto-restart triggered", f"{label}: {msg}")]
        except Exception as exc:
            msg = f"failed to launch {cmd}: {exc}"
            log.error("auto-restart %s -> %s", label, msg)
            return [event("crit", "Auto-restart failed", f"{label}: {msg}")]
