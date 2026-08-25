"""W22 — nodriver / Scrapling / Camoufox techniques ported: the pure logic, no network.

Pins: the httpx header set is a complete, self-consistent Chrome identity that matches the
curl_cffi impersonation target; Cloudflare walls are classified from the page the way
Scrapling does it; `is_block` escalates on those classes; the Camoufox fingerprint is
frozen on disk after the first roll; curl_cffi retries exactly once on a connection-level
error and never on a status.
"""
import asyncio
import json

import pytest

from webscraper import browser_fetch as bf
from webscraper import enrich
from webscraper import impersonate_fetch as imp
from webscraper.camoufox_fetch import OPTS_FILE, frozen_launch_options


# ── A. httpx header set ───────────────────────────────────────────────────────────────
def test_headers_are_a_complete_chrome_identity():
    h = enrich.HEADERS
    for k in ("User-Agent", "Accept", "Accept-Language", "Accept-Encoding", "sec-ch-ua",
              "sec-ch-ua-mobile", "sec-ch-ua-platform", "Sec-Fetch-Site", "Sec-Fetch-Mode",
              "Sec-Fetch-User", "Sec-Fetch-Dest", "Upgrade-Insecure-Requests", "Referer"):
        assert k in h, k
    assert h["Sec-Fetch-Site"] == "none" and h["Sec-Fetch-Mode"] == "navigate"
    assert h["Sec-Fetch-User"] == "?1" and h["Sec-Fetch-Dest"] == "document"
    assert h["sec-ch-ua-mobile"] == "?0" and h["Upgrade-Insecure-Requests"] == "1"
    assert h["Accept-Language"] == "en-GB,en;q=0.9"
    assert h["Referer"] == "https://www.google.com/"
    # Internally consistent: the UA's major version is the one sec-ch-ua claims, and the
    # platform hint matches the UA's OS token.
    assert f"Chrome/{enrich.CHROME_MAJOR}." in h["User-Agent"]
    assert f'"Google Chrome";v="{enrich.CHROME_MAJOR}"' in h["sec-ch-ua"]
    assert h["sec-ch-ua-platform"] == '"macOS"' and "Macintosh" in h["User-Agent"]
    # Only advertise encodings httpx can decode here.
    assert h["Accept-Encoding"].startswith("gzip, deflate")
    for enc, mod in (("br", "brotli"), ("zstd", "zstandard")):
        try:
            __import__(mod)
            assert enc in h["Accept-Encoding"]
        except ImportError:
            assert enc not in h["Accept-Encoding"].split(", ")


def test_ua_matches_impersonate_target():
    # curl_cffi's `chrome` alias resolves to chrome150 (0.16.x); the httpx UA must be the
    # same major so the two non-browser tiers present one identity.
    try:
        from curl_cffi.requests.impersonate import DEFAULT_CHROME
    except ImportError:
        pytest.skip("curl_cffi not installed")
    assert DEFAULT_CHROME.removeprefix("chrome") == enrich.CHROME_MAJOR
    assert imp.EXTRA_HEADERS["Referer"] == enrich.HEADERS["Referer"]
    assert imp.EXTRA_HEADERS["Accept-Language"] == enrich.HEADERS["Accept-Language"]


# ── C4. Cloudflare classification ──────────────────────────────────────────────────────
@pytest.mark.parametrize("html,kind", [
    ("<html><script>window._cf_chl_opt={cType: 'non-interactive',cRay:'8'}</script>", "non-interactive"),
    ("<title>Just a moment...</title><script>{cType: 'managed', cNounce: 1}</script>", "managed"),
    ("<script>_cf_chl_opt = { cType: 'interactive' }</script>", "interactive"),
    ('<form><script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async></script></form>',
     "embedded"),
])
def test_detect_cloudflare_classes(html, kind):
    assert bf.detect_cloudflare(html) == kind


def test_detect_cloudflare_none_on_plain_pages():
    assert bf.detect_cloudflare("<html><body>Contact hi@x.test</body></html>") is None
    assert bf.detect_cloudflare("<title>Access denied</title>") is None
    assert bf.detect_cloudflare(None) is None
    assert bf.cf_error("non-interactive") == "cf_non_interactive"
    assert bf.cf_error("managed") == "cf_managed"
    assert bf.cf_error(None) == "blocked"


def test_is_block_accepts_cf_classes():
    for e in ("cf_non_interactive", "cf_managed", "cf_interactive", "cf_embedded", "blocked",
              "cf_managed@gw.test:7777"):
        assert enrich.is_block(e), e
    assert not enrich.is_block("http_404") and not enrich.is_block("dns")


def test_launch_args_follow_the_nodriver_and_patchright_findings():
    joined = " ".join(bf.LAUNCH_ARGS)
    assert "IsolateOrigins" not in joined and "site-per-process" not in joined
    assert "--lang=en-GB" in bf.LAUNCH_ARGS and "--accept-lang=en-GB,en" in bf.LAUNCH_ARGS
    assert bf.AUTOMATION_FLAG not in bf.LAUNCH_ARGS       # only added when NOT patchright
    assert "--enable-automation" in bf.IGNORE_DEFAULT_ARGS
    # Default ON (user directive 2026-08-25) unless the env explicitly turns it off.
    env = (bf.os.getenv("ENRICH_CF_CLICK") or "").strip().lower()
    assert bf.CF_CLICK is (env not in ("0", "false", "no", "off"))
    assert bf.CHALLENGE_WAIT_MS == 12_000


def test_crawl_site_carries_cf_class_into_reason_and_camoufox_into_via(monkeypatch):
    monkeypatch.setattr(enrich, "get_pool", lambda: None)

    async def home(client, website, proxy=None):
        return enrich.Fetched(error="http_403", url="https://x.test")

    async def tls(url, proxy=None):
        return None, "http_403"
    monkeypatch.setattr(enrich, "_fetch_home", home)
    monkeypatch.setattr(enrich, "impersonate_fetch_ex", tls)

    async def browser(url):
        return None, None, "cf_managed"
    c, reason = asyncio.run(enrich.crawl_site(None, "x.test", browser))
    assert c.pages_fetched == 0 and reason == "cf_managed"

    async def camoufox_off(url):
        return None, "off"
    c, reason = asyncio.run(enrich.crawl_site(None, "x.test", browser, camoufox_off))
    assert reason == "cf_managed"            # 'off' never overwrites the real reason

    async def camoufox_ok(url):
        return "<html><body>hi@x.test</body></html>", None
    c, reason = asyncio.run(enrich.crawl_site(None, "x.test", browser, camoufox_ok))
    assert reason is None and c.via == "camoufox" and "hi@x.test" in c.emails


# ── D. Camoufox fingerprint freeze ─────────────────────────────────────────────────────
def test_camoufox_opts_are_frozen_on_first_use(tmp_path):
    calls = []

    def fake_launch_options(**kw):
        calls.append(kw)
        return {"executable_path": "x", "args": [], "env": {"CAMOU_CONFIG_1": f"fp{len(calls)}"},
                "headless": kw["headless"], "proxy": None}

    a = frozen_launch_options(tmp_path, True, fake_launch_options)
    assert calls[0]["humanize"] is False and calls[0]["block_images"] is True
    assert calls[0]["os"] in ("windows", "macos") and calls[0]["i_know_what_im_doing"] is True
    assert (tmp_path / OPTS_FILE).exists()
    b = frozen_launch_options(tmp_path, False, fake_launch_options)
    assert len(calls) == 1                               # second call = cache hit, no re-roll
    assert b["env"] == a["env"] == {"CAMOU_CONFIG_1": "fp1"}
    assert a["headless"] is True and b["headless"] is False   # headless re-applied per launch
    assert json.loads((tmp_path / OPTS_FILE).read_text())["env"] == a["env"]


def test_camoufox_corrupt_cache_regenerates(tmp_path):
    (tmp_path / OPTS_FILE).write_text("{not json", encoding="utf-8")
    got = frozen_launch_options(tmp_path, True, lambda **kw: {"env": {"k": "v"}, "headless": True})
    assert got["env"] == {"k": "v"}


# ── B. curl_cffi retry-once ────────────────────────────────────────────────────────────
class _Resp:
    status_code = 200
    headers = {"content-type": "text/html"}
    text = "<html><body>hello@x.test</body></html>"


def _session_class(script):
    """A fake AsyncSession whose .get() pops the next item of `script`: an exception to raise
    or a response to return. Records the kwargs the session was built with."""
    built = []

    class S:
        def __init__(self, **kw):
            built.append(kw)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, allow_redirects=True):
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
    S.built = built
    return S


def test_curl_error_retried_once_then_succeeds(monkeypatch):
    monkeypatch.setattr(imp, "ENABLED", True)
    monkeypatch.setattr(imp, "RETRY_DELAY_SEC", 0)
    script = [imp.CurlError("curl: (56) Recv failure"), _Resp()]
    S = _session_class(script)
    monkeypatch.setattr(imp, "_session_factory", lambda: S)
    html, err = asyncio.run(imp.impersonate_fetch_ex("https://x.test/", proxy=""))
    assert html and err is None and script == []
    assert S.built[0]["headers"]["Referer"] == "https://www.google.com/"


def test_curl_error_not_retried_twice(monkeypatch):
    monkeypatch.setattr(imp, "ENABLED", True)
    monkeypatch.setattr(imp, "RETRY_DELAY_SEC", 0)
    script = [imp.CurlError("curl: (7) Failed to connect"), imp.CurlError("curl: (7) again"), _Resp()]
    monkeypatch.setattr(imp, "_session_factory", lambda: _session_class(script))
    html, err = asyncio.run(imp.impersonate_fetch_ex("https://x.test/", proxy=""))
    assert html is None and err == "network" and len(script) == 1   # the 3rd item never ran


def test_http_403_is_never_retried(monkeypatch):
    monkeypatch.setattr(imp, "ENABLED", True)

    class Forbidden(_Resp):
        status_code = 403
    script = [Forbidden(), _Resp()]
    monkeypatch.setattr(imp, "_session_factory", lambda: _session_class(script))
    html, err = asyncio.run(imp.impersonate_fetch_ex("https://x.test/", proxy=""))
    assert html is None and err == "http_403" and len(script) == 1
