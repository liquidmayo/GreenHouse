"""Status-transition notifier: watches the store and pushes alerts when a
component crosses into an alertable state (or recovers), or when a remote
agent goes offline / comes back.

Rules (all configurable in monitors.yml `notify:`):
  - `min_severity` (default crit): only crit transitions alert; set warn to
    include warns. Recovery notices are sent for anything previously alerted.
  - `confirm_seconds` (default 45): a bad state must persist this long
    before alerting â€” filters one-cycle blips.
  - `offline_grace_seconds` (default 300): a machine must be silent this
    long before an offline alert (internet blips on remote agents are common).
  - `cooldown_minutes` (default 15): per-key minimum gap between repeat
    alerts of the same problem (a flapping service alerts once per cooldown).
  - `startup_grace_seconds` (default 90): nothing fires right after start,
    so a dashboard restart doesn't page you for pre-existing conditions
    (they show on the dashboard; the *next* transition alerts normally).

Channels: `discord_webhook` (embeds), `webhook` (generic JSON POST).
Notification failures are logged, never raised.
"""
import logging
import threading
import time

import requests

log = logging.getLogger("ghmon.notify")

SEV = {"ok": 0, "unknown": 0, "warn": 1, "crit": 2}
COLORS = {"crit": 0xEF4444, "warn": 0xEAB308, "ok": 0x22C55E, "offline": 0xEF4444}


class Notifier:
    def __init__(self, cfg, store):
        self.cfg = cfg or {}
        self.store = store
        self.enabled = bool(self.cfg.get("discord_webhook") or self.cfg.get("webhook"))
        self.min_sev = SEV.get(self.cfg.get("min_severity", "crit"), 2)
        self.confirm = float(self.cfg.get("confirm_seconds", 45))
        self.offline_grace = float(self.cfg.get("offline_grace_seconds", 300))
        self.cooldown = float(self.cfg.get("cooldown_minutes", 15)) * 60
        self.startup_grace = float(self.cfg.get("startup_grace_seconds", 90))
        self.started = time.time()
        # key -> {"since": ts bad state began, "alerted": bool, "last_sent": ts, "level": str}
        self.tracked = {}
        self._stop = threading.Event()

    # ------------------------------------------------------------ evaluation

    def evaluate(self):
        """Run one pass: compare current state to tracked state, send as needed."""
        now = time.time()
        state = self.store.state()
        seen = set()

        for machine, m in state.get("machines", {}).items():
            # machine-level offline
            key = f"{machine}::__agent__"
            seen.add(key)
            self._track(key, now, bad=m.get("offline", False), level="offline",
                        title=f"{machine} agent OFFLINE",
                        detail=f"no reports for {m.get('agent_age_s')}s",
                        recover_title=f"{machine} agent back online",
                        confirm=self.offline_grace)
            if m.get("offline"):
                continue  # component states are stale while offline; skip them
            for comp in m.get("components", []):
                key = f"{machine}::{comp['id']}"
                seen.add(key)
                status = comp.get("status", "unknown")
                bad = SEV.get(status, 0) >= self.min_sev
                self._track(key, now, bad=bad, level=status,
                            title=f"{machine} / {comp['label']} is {status.upper()}",
                            detail=comp.get("summary") or "",
                            recover_title=f"{machine} / {comp['label']} recovered",
                            confirm=self.confirm)

        # forget keys that vanished (config edits) so they don't linger
        for key in list(self.tracked):
            if key not in seen:
                del self.tracked[key]

    def _track(self, key, now, bad, level, title, detail, recover_title, confirm):
        t = self.tracked.get(key)
        if bad:
            if t is None:
                self.tracked[key] = {"since": now, "alerted": False,
                                     "last_sent": 0.0, "level": level}
                return
            t["level"] = level
            persisted = now - t["since"] >= confirm
            in_grace = now - self.started < self.startup_grace
            cooled = now - t["last_sent"] >= self.cooldown
            if persisted and not in_grace and cooled and not t["alerted"]:
                self._send(level, title, detail)
                t["alerted"] = True
                t["last_sent"] = now
        else:
            if t is not None:
                if t["alerted"]:
                    self._send("ok", recover_title, "")
                del self.tracked[key]

    # ------------------------------------------------------------ delivery

    def _send(self, level, title, detail):
        if not self.enabled:
            return
        log.info("notify [%s] %s â€” %s", level, title, detail)
        payload = {"level": level, "title": title, "detail": detail,
                   "ts": time.time(), "source": "greenhouse-monitor"}
        dw = self.cfg.get("discord_webhook")
        if dw:
            icon = {"crit": "ðŸ”´", "warn": "ðŸŸ¡", "ok": "ðŸŸ¢", "offline": "âš«"}.get(level, "âšª")
            embed = {
                "title": f"{icon} {title}",
                "description": detail[:1500] if detail else "",
                "color": COLORS.get(level, 0x808080),
                "footer": {"text": "GreenHouse Monitor"},
            }
            self._post(dw, {"embeds": [embed]}, "discord")
        gw = self.cfg.get("webhook")
        if gw:
            self._post(gw, payload, "webhook")

    @staticmethod
    def _post(url, body, label):
        try:
            resp = requests.post(url, json=body, timeout=8)
            if resp.status_code >= 300:
                log.warning("%s notification returned HTTP %s: %s",
                            label, resp.status_code, resp.text[:200])
        except Exception as exc:
            log.warning("%s notification failed: %s", label, exc)

    # ------------------------------------------------------------ lifecycle

    def start_background(self, interval=10):
        if not self.enabled:
            log.info("notifications disabled (no notify.discord_webhook/webhook configured)")
            return None

        def loop():
            log.info("notifier started: min_severity=%s confirm=%ss offline_grace=%ss "
                     "cooldown=%smin", self.cfg.get("min_severity", "crit"),
                     self.confirm, self.offline_grace, self.cooldown / 60)
            while not self._stop.wait(interval):
                try:
                    self.evaluate()
                except Exception:
                    log.exception("notifier evaluation failed")

        thread = threading.Thread(target=loop, name="gh-notifier", daemon=True)
        thread.start()
        return thread

    def stop(self):
        self._stop.set()

    def test(self):
        """Send a one-off test message on all configured channels."""
        self._send("ok", "GreenHouse Monitor notifications are working",
                   "This is a test message from the dashboard.")
