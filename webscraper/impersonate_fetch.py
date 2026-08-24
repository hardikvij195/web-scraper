"""Fetch one page with a real Chrome TLS/HTTP2 fingerprint, for sites that 403 plain httpx.

Why this exists: live CRM job #7 finished with 15 leads at `enrich_status='http_403'`. Many
Cloudflare/WAF rules reject on the TLS + HTTP2 handshake fingerprint alone — httpx's is not a
browser's, so it is refused before a single byte of the page is served, while a real Chrome
gets 200. `curl_cffi` replays Chrome's exact JA3/HTTP2 fingerprint, so those sites answer it
the same as a browser would, at httpx speed (~0.3 s) instead of the browser slow path (~5 s).

Where it sits: BETWEEN httpx and the headless browser in `enrich.crawl_site`. httpx handles
the 90 % that never block; this rescues the fingerprint-403s cheaply; the browser is left for
the JS interactive challenges only it can solve. It changes nothing for a site httpx already
reads.

Scope (deliberately narrow): a real Chrome making an ordinary GET of a PUBLIC business page.
No proxies, no identity rotation, no CAPTCHA solving — those were considered and deferred. A
site behind a full interactive challenge still says no here, and that is left to the browser
or, ultimately, residential proxies.

Optional dependency: if `curl_cffi` is not installed the whole module is a no-op that returns
None, so enrichment behaves exactly as it did before — same contract as `browser_fetch`.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable

from webscraper.config import _bool

log = logging.getLogger("webscraper.impersonate_fetch")

#: Default ON — it is cheap and only ever runs on a site httpx already failed to read.
ENABLED = _bool(os.getenv("ENRICH_TLS_IMPERSONATE"), True)

#: Which browser fingerprint curl_cffi replays. A recent stable Chrome is the safest default;
#: override with ENRICH_TLS_IMPERSONATE_TARGET if a newer one lands in curl_cffi.
IMPERSONATE = os.getenv("ENRICH_TLS_IMPERSONATE_TARGET") or "chrome"

NAV_TIMEOUT_SEC = 20.0
MAX_BYTES = 1_500_000

#: Same challenge/deny signatures the browser path checks — a 200 whose body is one of these
#: is the WAF interstitial, not the business site, so it must NOT count as a success.
_BLOCK_MARKERS = (
    "just a moment", "attention required! | cloudflare", "checking your browser",
    "access denied", "error 1015", "request blocked", "are you a robot",
    "enable javascript and cookies to continue", "ddos protection by",
)


def usable_html(html: str | None) -> str | None:
    """The page's HTML if it is really the site, or None if it is empty or a challenge page.

    Cheaper and more honest than trusting the status: a WAF answers 200 with an interstitial,
    so the body — not the code — is what tells a rescue from a wall."""
    if not html:
        return None
    head = html[:4000].lower()
    if any(m in head for m in _BLOCK_MARKERS):
        return None
    return html[:MAX_BYTES]


def _session_factory() -> Callable[..., Any] | None:
    """Return curl_cffi's AsyncSession class, or None when the optional dep is not installed.

    Isolated into one function so a test can monkeypatch it and exercise the degrade path
    without uninstalling anything."""
    try:
        from curl_cffi.requests import AsyncSession  # type: ignore
    except Exception as e:  # noqa: BLE001 — any import failure = feature simply off
        log.debug("curl_cffi unavailable, TLS impersonation off: %s", e)
        return None
    return AsyncSession


async def impersonate_fetch(url: str) -> str | None:
    """HTML of `url` fetched with a Chrome fingerprint, or None if that did not get it either.

    Never raises: a failure here just means the caller falls through to the browser path, so
    every error becomes None, exactly like `browser_fetch.fetch`."""
    if not ENABLED:
        return None
    session_cls = _session_factory()
    if session_cls is None:
        return None
    try:
        async with session_cls(impersonate=IMPERSONATE, timeout=NAV_TIMEOUT_SEC) as s:
            r = await s.get(url, allow_redirects=True)
            if r.status_code >= 400:
                log.debug("tls impersonate still refused %s: %s", url, r.status_code)
                return None
            ctype = (r.headers.get("content-type") or "").lower()
            if ctype and "html" not in ctype and "xml" not in ctype:
                return None
            return usable_html(r.text)
    except Exception as e:  # noqa: BLE001 — fall through to the browser, never crash the run
        log.debug("tls impersonate failed %s: %s", url, e)
        return None
