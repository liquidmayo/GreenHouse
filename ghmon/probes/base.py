"""Base probe plumbing shared by all probe types."""
import time

# Severity ordering used to aggregate probe results into a component status.
SEVERITY = {"ok": 0, "unknown": 0, "warn": 1, "crit": 2}


def result(status="ok", summary="", metrics=None, events=None):
    return {
        "status": status,
        "summary": summary,
        "metrics": metrics or {},
        "events": events or [],
    }


def event(level, label, message=""):
    return {
        "ts": time.time(),
        "level": level,
        "label": label,
        "message": (message or "")[:400],
    }


class Probe:
    """A probe checks one aspect of a component and returns a result dict.

    Probe instances persist across collection cycles so they can keep state
    (log file positions, previous counter values, cached auth tokens).

    Any probe may set `every: <seconds>` in its config to run at most that
    often — between runs the last result is returned (useful for heavy or
    rate-limited targets).
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self._last_run = 0.0
        self._last_result = None

    def run(self):
        raise NotImplementedError

    def safe_run(self):
        every = self.cfg.get("every")
        now = time.time()
        if every and self._last_result is not None and now - self._last_run < every:
            return self._last_result
        try:
            res = self.run()
        except Exception as exc:  # a broken probe must never kill the cycle
            res = result(
                "warn",
                f"probe error: {exc}",
                events=[event("warn", "Probe error", f"{self.cfg.get('type')}: {exc}")],
            )
        self._last_run = now
        self._last_result = res
        return res


def worst(statuses):
    agg = "unknown"
    rank = -1
    for st in statuses:
        sev = SEVERITY.get(st, 0)
        if st == "unknown" and rank >= 0:
            continue
        if sev > rank or rank < 0:
            # prefer concrete states over unknown at equal severity
            if not (st == "unknown" and agg != "unknown" and sev <= rank):
                rank = sev
                agg = st
    # any concrete status beats unknown
    concrete = [s for s in statuses if s != "unknown"]
    if concrete:
        agg = max(concrete, key=lambda s: SEVERITY.get(s, 0))
    return agg
