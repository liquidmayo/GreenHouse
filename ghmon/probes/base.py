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
    """

    def __init__(self, cfg):
        self.cfg = cfg

    def run(self):
        raise NotImplementedError

    def safe_run(self):
        try:
            return self.run()
        except Exception as exc:  # a broken probe must never kill the cycle
            return result(
                "warn",
                f"probe error: {exc}",
                events=[event("warn", "Probe error", f"{self.cfg.get('type')}: {exc}")],
            )


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
