"""Facebook public-page follower count via headless Chromium (Playwright).

Fallback for pages this profile cannot get a Graph API token for: loads the
public page logged out — the new page layout renders the EXACT count
(e.g. "60,623 followers") — and parses it from the rendered HTML. A plain
HTTP fetch gets a login-wall stub, hence the real browser engine.

Needs: pip install playwright && python -m playwright install chromium.
A run costs a few seconds of headless browser, so poll gently and pair with
hold_metrics:

    - type: fb_public
      page: SomePublicPage
      every: 900
      timeout: 30
      level_if_down: unknown
      hold_metrics: 21600

Only an exact comma-grouped number is accepted — if Facebook serves a
rounded "60K" (or a login wall), the probe reports no metric rather than a
wrong one, and hold_metrics carries the last good count.
"""
import os
import re

from .base import Probe, result

# Browsers live inside the project, NOT the default %LOCALAPPDATA% — installs
# run from an MSIX-packaged shell (e.g. the Claude desktop app) get their
# AppData virtualized into the app's package overlay, invisible to normally
# launched processes (logon task, keepalive). A plain D:\ path is seen
# identically by every context. Install with:
#   set PLAYWRIGHT_BROWSERS_PATH=<root>\pw-browsers
#   python -m playwright install chromium
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BROWSERS = os.path.join(_ROOT, "pw-browsers")
if os.path.isdir(_BROWSERS):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", _BROWSERS)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class FbPublicProbe(Probe):
    def run(self):
        from playwright.sync_api import sync_playwright
        page_name = self.cfg["page"]
        timeout_ms = int(self.cfg.get("timeout", 30)) * 1000
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"])
            try:
                ctx = browser.new_context(user_agent=UA, locale="en-US",
                                          viewport={"width": 1280, "height": 900})
                pg = ctx.new_page()
                pg.goto(f"https://www.facebook.com/{page_name}",
                        timeout=timeout_ms, wait_until="domcontentloaded")
                try:
                    pg.wait_for_selector("text=/[0-9][0-9,.]* followers/i",
                                         timeout=timeout_ms)
                except Exception:
                    pass          # fall through — parse whatever rendered
                html = pg.content()
            finally:
                browser.close()
        m = re.search(r"([0-9][0-9,]*) followers", html)
        if not m:
            level = self.cfg.get("level_if_down", "unknown")
            walled = len(html) < 5000 or "log in" in html[:3000].lower()
            return result(level, "no exact follower count on public page"
                          + (" (login wall?)" if walled else ""))
        n = int(m.group(1).replace(",", ""))
        return result("ok", f"{page_name}: {n} followers",
                      metrics={"followers": n})
