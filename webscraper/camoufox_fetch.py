"""W22 — the LAST fetch tier: Camoufox, a Firefox whose fingerprint is rewritten in C++.

Where it sits: after httpx → curl_cffi → real Chrome (`browser_fetch`) have ALL come back
block-shaped. Chrome + patchright still leaks a few surfaces Cloudflare scores (canvas,
WebGL renderer, the headless UA/screen mismatch); Camoufox rewrites those below the JS
layer, so a wall that Chrome could not ride out sometimes clears here. It is a different
browser engine, a ~300 MB binary and a ~3 s launch, so it is env-gated OFF
(`ENRICH_BROWSER_CAMOUFOX=1`) and tried ONCE per site, with no proxy rotation.

Frozen fingerprint: `camoufox.utils.launch_options` rolls a fresh fingerprint on every
call. A `cf_clearance` cookie is bound to the fingerprint that earned it, so a profile whose
identity changes each launch throws its own clearance away. The options dict is therefore
written to `<profile>/camoufox-opts.json` on first use and reused verbatim after that
(only `headless` is re-applied per launch). Delete the file to roll a new identity.

Stock Playwright drives it (`playwright.sync_api`, NOT patchright — patchright is a
Chromium-only patch set and its Firefox is just stock Playwright's anyway). Everything
else — worker thread, queue, relaunch, settle/poll, Cloudflare classification — is
inherited from `BrowserFetcher`. Import of `camoufox` is lazy; if it is missing the tier
is skipped with one log line.

Deliberately NOT used: `humanize` (cursor animation — costs seconds per page and we never
interact), `geoip` (needs a MaxMind download and a proxy to be meaningful), `virtual_display`.
"""
from __future__ import annotations

import json
import logging
import os
import platform
from pathlib import Path
from typing import Any, Callable

from webscraper.browser_fetch import NAV_TIMEOUT_MS, BrowserFetcher
from webscraper.config import _bool, settings

log = logging.getLogger("webscraper.camoufox_fetch")

#: Default OFF — a second browser engine on the agent PC is the user's call.
CAMOUFOX_ENABLED = _bool(os.getenv("ENRICH_BROWSER_CAMOUFOX"), False)
PROFILE_DIR = settings.profile_dir.parent / "camoufox-profile"
OPTS_FILE = "camoufox-opts.json"


def available() -> bool:
    """True when the `camoufox` package imports. The binary itself is checked at launch."""
    try:
        import camoufox.utils  # noqa: F401
    except Exception as e:  # noqa: BLE001 — not installed / broken install = tier off
        log.info("camoufox not available (%s) — tier skipped", e)
        return False
    return True


def _os_name() -> str:
    return "macos" if platform.system() == "Darwin" else "windows"


def frozen_launch_options(profile_dir: Path, headless: bool,
                          launch_options: Callable[..., dict[str, Any]] | None = None,
                          ) -> dict[str, Any]:
    """The camoufox launch options for this profile: generated once, then read back from
    `<profile>/camoufox-opts.json` so the fingerprint (and with it any cf_clearance cookie
    the profile earned) stays stable across launches. `headless` is applied per call."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    path = profile_dir / OPTS_FILE
    if path.exists():
        try:
            opts = json.loads(path.read_text(encoding="utf-8"))
            opts["headless"] = headless
            return opts
        except (OSError, ValueError) as e:
            log.warning("camoufox opts cache unreadable (%s) — regenerating", e)
    if launch_options is None:
        from camoufox.utils import launch_options as _lo
        launch_options = _lo
    opts = launch_options(headless=headless, os=_os_name(), humanize=False, block_images=True,
                          i_know_what_im_doing=True)
    opts = json.loads(json.dumps(opts))          # plain JSON types only, same as the cache
    try:
        path.write_text(json.dumps(opts, indent=1), encoding="utf-8")
    except OSError as e:
        log.warning("could not freeze camoufox opts (%s) — fingerprint will roll per launch", e)
    return opts


class CamoufoxFetcher(BrowserFetcher):
    """`BrowserFetcher` driven through Camoufox's Firefox instead of Chrome. Same `fetch_ex`
    contract; `via='camoufox'` is the caller's label."""

    def _playwright(self) -> Any:
        from playwright.sync_api import sync_playwright   # stock, not patchright
        return sync_playwright()

    def _profile_dir(self) -> Path:
        return PROFILE_DIR

    def _opener(self, pw: Any) -> Callable[[], tuple[Any, Any]]:
        from webscraper.browser_fetch import _proxy_arg
        proxy = _proxy_arg(self._proxy)

        def _open() -> tuple[Any, Any]:
            opts = frozen_launch_options(PROFILE_DIR, self._headless)
            opts.pop("proxy", None)
            if proxy:
                opts["proxy"] = proxy
            ctx = pw.firefox.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR), no_viewport=True, ignore_https_errors=True,
                **opts)
            log.debug("camoufox launched (headless=%s, proxy=%s)", self._headless,
                      proxy["server"] if proxy else None)
            self._route_assets(ctx)
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg.set_default_timeout(NAV_TIMEOUT_MS)
            return ctx, pg
        return _open
