"""TLS-impersonation fetch — the pure logic, no network.

`impersonate_fetch` sends a real Chrome TLS/HTTP2 handshake (via curl_cffi) to sites that
403 httpx on its fingerprint alone. These tests pin the two behaviours that must hold with
no network and no optional dependency installed: the challenge-page gate (a 200 that is
really a Cloudflare interstitial is NOT a success), and graceful degradation when curl_cffi
is absent (the whole feature turns into a no-op, exactly like the browser fallback does).
"""
import asyncio

from webscraper import impersonate_fetch as imp


def test_accepts_real_html():
    assert imp.usable_html("<html><body>Contact us: hi@shop.co.uk</body></html>") is not None


def test_rejects_challenge_pages():
    # A 200 whose body is a Cloudflare/Datadome interstitial is not the business site.
    assert imp.usable_html("<title>Just a moment...</title>") is None
    assert imp.usable_html("<h1>Attention Required! | Cloudflare</h1>") is None
    assert imp.usable_html("Checking your browser before accessing") is None
    assert imp.usable_html(None) is None
    assert imp.usable_html("") is None


def test_degrades_when_curl_cffi_missing(monkeypatch):
    # Force the optional import to fail; the fetch must return None, never raise, so a run on
    # a machine without curl_cffi behaves exactly as it did before this feature existed.
    monkeypatch.setattr(imp, "_session_factory", lambda: None)
    got = asyncio.run(imp.impersonate_fetch("https://example.test/"))
    assert got is None


def test_disabled_by_env(monkeypatch):
    # ENRICH_TLS_IMPERSONATE=false turns the whole path off before any import is attempted.
    monkeypatch.setattr(imp, "ENABLED", False)
    got = asyncio.run(imp.impersonate_fetch("https://example.test/"))
    assert got is None


def test_proxy_arg_parses_credentials(monkeypatch):
    # The browser proxy dict must split user/pass out of the URL for Playwright.
    from webscraper import browser_fetch as bf
    monkeypatch.setattr(bf.settings, "enrich_proxy", "http://u:p@gw.test:7777")
    assert bf._proxy_arg() == {"server": "http://gw.test:7777", "username": "u", "password": "p"}


def test_proxy_arg_none_when_unset(monkeypatch):
    from webscraper import browser_fetch as bf
    monkeypatch.setattr(bf.settings, "enrich_proxy", None)
    assert bf._proxy_arg() is None
