"""Facebook Page follower count via the official Meta Graph API.

For pages YOU admin: exact followers_count, no scraping, no third party.
Needs a Page access token (never-expiring when derived from a long-lived
user token).

    - type: fb_graph
      page_id: "123456789012345"   # numeric page id (or username)
      access_token: "EAAG..."      # that page's access token
      every: 300
      timeout: 10
      api_version: ""              # optional, e.g. v23.0; blank = default

Emits the same `followers` metric as the mixerno http_json probes, so hero
tiles, the ticker, trends, and the matrix keep working unchanged.
"""
import requests

from .base import Probe, result

GRAPH = "https://graph.facebook.com"


class FbGraphProbe(Probe):
    def run(self):
        page = self.cfg.get("page_id", "me")
        ver = (self.cfg.get("api_version") or "").strip("/")
        url = f"{GRAPH}/{ver}/{page}" if ver else f"{GRAPH}/{page}"
        r = requests.get(
            url, timeout=self.cfg.get("timeout", 10),
            params={"fields": "name,followers_count,fan_count",
                    "access_token": self.cfg.get("access_token", "")})
        d = r.json() if r.content else {}
        if "error" in d:
            err = d["error"]
            # expired/invalid token (code 190) needs a human — make it loud
            msg = f"Graph API error {err.get('code')}: {err.get('message', '')[:120]}"
            level = "warn" if err.get("code") == 190 else \
                self.cfg.get("level_if_down", "warn")
            return result(level, msg)
        r.raise_for_status()
        followers = d.get("followers_count")
        if not isinstance(followers, (int, float)):
            return result(self.cfg.get("level_if_down", "warn"),
                          f"no followers_count in reply for {d.get('name', page)}")
        metrics = {"followers": int(followers)}
        if isinstance(d.get("fan_count"), (int, float)):
            metrics["likes"] = int(d["fan_count"])
        return result("ok", f"{d.get('name', page)}: {int(followers)} followers",
                      metrics=metrics)
