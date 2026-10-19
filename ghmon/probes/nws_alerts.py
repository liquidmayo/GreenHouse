"""NWS active watches/warnings for configured counties, via api.weather.gov.

Free public API, no key; NWS asks for an identifying User-Agent. Queries the
state's active alerts and keeps only Actual watches/warnings whose area
mentions one of the configured counties. Advisories/statements are ignored.

    - type: nws_alerts
      area: OH
      counties: [Mercer, Darke, Auglaize, Shelby]
      every: 120
      timeout: 10
      level_when_active: ok      # or warn, to color the dashboard card

Metrics: nws_active (count) and nws_list — a list of
{id, event, severity, counties, headline, expires_ts} dicts that the ticker
endpoint forwards to embedded displays.
"""
from datetime import datetime

import requests

from .base import Probe, result

USER_AGENT = "ghmon (set your contact here)"


def _iso_to_epoch(s):
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


class NwsAlertsProbe(Probe):
    def run(self):
        area = self.cfg.get("area", "OH")
        counties = [str(c).lower() for c in self.cfg.get("counties", [])]
        r = requests.get(
            f"https://api.weather.gov/alerts/active?area={area}",
            timeout=self.cfg.get("timeout", 10),
            headers={"User-Agent": self.cfg.get("user_agent", USER_AGENT),
                     "Accept": "application/geo+json"})
        r.raise_for_status()
        alerts = []
        for feat in r.json().get("features", []):
            p = feat.get("properties", {})
            event = p.get("event", "")
            if p.get("status") != "Actual":
                continue
            if not event.endswith(("Watch", "Warning")):
                continue
            area_desc = (p.get("areaDesc") or "").lower()
            hit = [c for c in counties if c in area_desc]
            if counties and not hit:
                continue
            alerts.append({
                "id": p.get("id") or feat.get("id", ""),
                "event": event,
                "severity": p.get("severity"),
                "counties": ", ".join(c.title() for c in hit),
                "headline": (p.get("headline") or "")[:200],
                "expires_ts": _iso_to_epoch(p.get("expires") or ""),
            })
        # warnings ahead of watches, then alphabetical
        alerts.sort(key=lambda a: (0 if a["event"].endswith("Warning") else 1,
                                   a["event"]))
        n = len(alerts)
        if n:
            summary = f"{n} active: " + "; ".join(
                f"{a['event']} ({a['counties']})" for a in alerts[:4])
            level = self.cfg.get("level_when_active", "ok")
        else:
            summary = "No active watches/warnings"
            level = "ok"
        return result(level, summary[:220],
                      metrics={"nws_active": n, "nws_list": alerts})
