"""YouTube livestream probe: is a stream live, and how many are watching.

Scrapes the public watch page (no API key needed) for the isLiveNow flag
and the "watching now" count. The page is ~1 MB, so pair this with the
probe-level `every:` throttle.

Config:
  type: youtube_live
  video_id: X_0Y5F592uA        # or a full url: https://www.youtube.com/watch?v=...
  every: 60                    # scrape at most once a minute
  offline_level: warn          # component status when the stream is not live
  timeout: 15
"""
import re

import requests

from .base import Probe, result, event

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

RE_LIVE = re.compile(r'"isLiveNow"\s*:\s*(true|false)')
RE_VIEWERS = re.compile(r'"originalViewCount"\s*:\s*"(\d+)"')
RE_TITLE = re.compile(r"<title>([^<]*)</title>")


class YoutubeLiveProbe(Probe):
    def run(self):
        cfg = self.cfg
        url = cfg.get("url")
        if not url:
            url = f"https://www.youtube.com/watch?v={cfg['video_id']}"
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": UA, "Accept-Language": "en-US"},
                timeout=cfg.get("timeout", 15),
            )
            resp.raise_for_status()
        except Exception as exc:
            return result("unknown", f"YouTube unreachable: {exc.__class__.__name__}")

        page = resp.text
        live_match = RE_LIVE.search(page)
        if not live_match:
            # Page layout changed or a consent/blocked page was served —
            # don't guess, and don't alarm on our own parsing failure.
            return result("unknown", "could not parse live status",
                          events=[event("warn", "YouTube parse failure",
                                        "isLiveNow marker not found in watch page")])

        is_live = live_match.group(1) == "true"
        metrics = {"live": 1 if is_live else 0}
        if is_live:
            viewers = RE_VIEWERS.search(page)
            if viewers:
                metrics["viewers"] = int(viewers.group(1))
            return result("ok", "", metrics)

        level = cfg.get("offline_level", "warn")
        title_match = RE_TITLE.search(page)
        title = title_match.group(1).replace(" - YouTube", "").strip() if title_match else "stream"
        return result(level, "stream is not live", metrics,
                      [event(level, "YouTube stream offline", title)])
