"""W56 (CRM T398) — what the blocked-sites audit changed, pinned without any network.

Audit over 19,463 enriched leads (2026-09-07): 661 dns, 435 'network', 164 404, 159 timeout,
93 'blocked', 89 403, ~50 5xx, 32 Cloudflare walls, and 1,852 leads marked *done* with
nothing extracted. Each test below is one of the fixes that came out of it."""
import asyncio

import httpx

from webscraper import enrich, impersonate_fetch as imp
from webscraper.browser_fetch import cf_error, detect_cloudflare, looks_blocked
from webscraper.enrich import (Fetched, HOST_ERRORS, _www_variant, classify_deny, is_block, is_ip_deny,
                               transport_error)
from webscraper.extractors import contact_page_links, extract_structured, is_js_shell


# ── 'network' split into tls / reset / refused ──────────────────────────────
def test_transport_error_subclasses():
    assert transport_error(httpx.ConnectError("[SSL: WRONG_VERSION_NUMBER] wrong version number")) == "tls"
    assert transport_error(httpx.ConnectError("certificate verify failed")) == "tls"
    assert transport_error(httpx.ReadError("[WinError 10054] An existing connection was forcibly closed")) == "reset"
    assert transport_error(httpx.RemoteProtocolError("Server disconnected without sending a response.")) == "reset"
    assert transport_error(httpx.ConnectError("[WinError 10061] No connection could be made because the target machine actively refused it")) == "refused"
    assert transport_error(httpx.ConnectError("getaddrinfo failed")) == "dns"      # unchanged
    assert transport_error(httpx.ConnectTimeout("timed out")) == "timeout"        # unchanged
    assert transport_error(httpx.ConnectError("something else entirely")) == "network"
    for e in ("dns", "tls", "reset", "refused", "timeout"):
        assert e in HOST_ERRORS


# ── Cloudflare 1020 / 1015 = a deny, not a challenge ────────────────────────
CF_1020 = "<html><head><title>Access denied | shop.example.com used Cloudflare to restrict access</title></head><body><h1>Error 1020</h1><p>Access denied</p><span>Error code: 1020</span></body></html>"


def test_classify_deny_recognises_cloudflare_deny_pages():
    assert classify_deny(403, CF_1020) == "cf_deny"
    assert classify_deny(429, "<h1>Error 1015</h1> You are being rate limited") == "cf_deny"
    assert classify_deny(403, "<html><body>Forbidden</body></html>") == "http_403"
    assert classify_deny(404, CF_1020) == "http_404"           # only block-shaped statuses
    assert classify_deny(403, None) == "http_403"


def test_cf_deny_is_not_escalated_but_is_a_deny():
    # No fingerprint / browser on the same IP changes a static deny — the ladder must not
    # spend ~5 s of Chrome on it (is_block False) — but it IS an IP deny a proxy could beat.
    assert not is_block("cf_deny")
    assert is_ip_deny("cf_deny")
    assert is_ip_deny("cf_deny@gw:7777")
    assert not is_ip_deny("http_403")
    assert is_block("http_403")                                   # unchanged


def test_browser_tier_classifies_deny_page():
    assert looks_blocked(CF_1020)
    assert detect_cloudflare(CF_1020) == "deny"
    assert cf_error("deny") == "cf_deny"
    assert detect_cloudflare("<html><body>welcome to our shop</body></html>") is None


# ── www. ⇄ bare host variant on host-level failures ─────────────────────────
def test_www_variant():
    assert _www_variant("https://www.example.co.uk/") == "https://example.co.uk/"
    assert _www_variant("https://example.co.uk/about") == "https://www.example.co.uk/about"
    assert _www_variant("https://shop.brand.example.com/") is None       # deeper subdomain — no guess
    assert _www_variant("http://192.168.0.1/") is None


class _FakeClient:
    """Scripted `_fetch_ex` answers keyed by URL; records the order tried."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []


def _scripted(client):
    async def fake_fetch_ex(_client, url, retries=1, proxy=None):
        client.calls.append(url)
        got = client.answers.get(url, Fetched(error="dns"))
        return Fetched(html=got.html, error=got.error)
    return fake_fetch_ex


def test_fetch_home_tries_other_hostname_on_dns(monkeypatch):
    fc = _FakeClient({"https://example.co.uk": Fetched(html="<html>hello there</html>")})
    monkeypatch.setattr(enrich, "_fetch_ex", _scripted(fc))
    got = asyncio.run(enrich._fetch_home(fc, "www.example.co.uk"))
    assert got.html and got.url == "https://example.co.uk"
    # http → https → the https variant; nothing more.
    assert fc.calls == ["http://www.example.co.uk", "https://www.example.co.uk", "https://example.co.uk"]


def test_fetch_home_does_not_guess_hostnames_on_a_403(monkeypatch):
    # A block is a verdict on the page, not the host — the variant would just be a 403 twice.
    fc = _FakeClient({"http://www.example.com": Fetched(error="http_403"), "https://www.example.com": Fetched(error="http_403")})
    monkeypatch.setattr(enrich, "_fetch_ex", _scripted(fc))
    got = asyncio.run(enrich._fetch_home(fc, "www.example.com"))
    assert got.error == "http_403"
    assert all("example.com" in u and "www." in u for u in fc.calls)


# ── 429 back-off ────────────────────────────────────────────────────────────
def test_429_backs_off_once_then_retries(monkeypatch):
    seq = [httpx.Response(429, headers={"retry-after": "1"}, request=httpx.Request("GET", "https://x.test/")),
           httpx.Response(200, headers={"content-type": "text/html"}, text="<html>ok</html>", request=httpx.Request("GET", "https://x.test/"))]
    slept = []

    class C:
        async def get(self, url):
            return seq.pop(0)

    async def fake_sleep(s):
        slept.append(s)
    monkeypatch.setattr(enrich.asyncio, "sleep", fake_sleep)
    got = asyncio.run(enrich._fetch_ex(C(), "https://x.test/", retries=0))
    assert got.html == "<html>ok</html>"
    assert slept == [1.0]


def test_429_twice_is_stored_as_429(monkeypatch):
    seq = [httpx.Response(429, request=httpx.Request("GET", "https://x.test/")) for _ in range(2)]

    class C:
        async def get(self, url):
            return seq.pop(0)

    async def fake_sleep(s):
        pass
    monkeypatch.setattr(enrich.asyncio, "sleep", fake_sleep)
    got = asyncio.run(enrich._fetch_ex(C(), "https://x.test/", retries=0))
    assert got.error == "http_429"


# ── TLS identity rotation ────────────────────────────────────────────────────
class _Resp:
    def __init__(self, status, text="<html><body>real site hello@x.test</body></html>"):
        self.status_code, self.text, self.headers = status, text, {"content-type": "text/html"}


def _session_that(answers, seen):
    class S:
        def __init__(self, **kw):
            self.target = kw["impersonate"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, allow_redirects=True):
            seen.append(self.target)
            return answers[self.target]
    return S


def test_impersonate_rotates_identity_on_403(monkeypatch):
    seen = []
    monkeypatch.setattr(imp, "ROTATION", ["chrome", "safari18_0", "firefox147"])
    monkeypatch.setattr(imp, "_session_factory", lambda: _session_that(
        {"chrome": _Resp(403), "safari18_0": _Resp(200), "firefox147": _Resp(200)}, seen))
    monkeypatch.setattr(imp.settings, "enrich_proxy", None)
    html, err = asyncio.run(imp.impersonate_fetch_ex("https://x.test/"))
    assert html and err is None
    assert seen == ["chrome", "safari18_0"]


def test_impersonate_does_not_rotate_on_404(monkeypatch):
    seen = []
    monkeypatch.setattr(imp, "ROTATION", ["chrome", "safari18_0"])
    monkeypatch.setattr(imp, "_session_factory", lambda: _session_that({"chrome": _Resp(404), "safari18_0": _Resp(200)}, seen))
    monkeypatch.setattr(imp.settings, "enrich_proxy", None)
    html, err = asyncio.run(imp.impersonate_fetch_ex("https://x.test/"))
    assert html is None and err == "http_404"
    assert seen == ["chrome"]


def test_impersonate_reports_last_identity_when_all_refuse(monkeypatch):
    seen = []
    monkeypatch.setattr(imp, "ROTATION", ["chrome", "safari18_0"])
    monkeypatch.setattr(imp, "_session_factory", lambda: _session_that({"chrome": _Resp(403), "safari18_0": _Resp(403)}, seen))
    monkeypatch.setattr(imp.settings, "enrich_proxy", None)
    html, err = asyncio.run(imp.impersonate_fetch_ex("https://x.test/"))
    assert html is None and err == "http_403"
    assert seen == ["chrome", "safari18_0"]


# ── JS-only shells ───────────────────────────────────────────────────────────
SHELL = '<!doctype html><html><head><meta charset="utf-8"><title>Shop</title><link rel="stylesheet" href="/app.css"></head><body><div id="root"></div><script src="/static/js/main.9f8e7d.js"></script>' + "<!-- " + "x" * 2000 + " --></body></html>"
REAL = '<html><body><div id="root"><h1>Acme Plumbing</h1><p>Call us on 020 7946 0000 or email hello@acme.example for a quote. We cover the whole of Greater London and have done since 1998.</p><a href="/contact">Contact</a></div><script src="/bundle.js"></script>' + "<!-- " + "x" * 2000 + " --></body></html>"


def test_is_js_shell():
    assert is_js_shell(SHELL)
    assert not is_js_shell(REAL)              # same mount node, but it has real text
    assert not is_js_shell("<html><body><div id='root'></div></body></html>")   # too small to be a bundle shell
    assert not is_js_shell(None)


# ── JSON-LD contact data ─────────────────────────────────────────────────────
LD = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"LocalBusiness","name":"Acme","email":"mailto:info@acme.example",
 "telephone":"+44 20 7946 0000","sameAs":["https://www.instagram.com/acmeplumb/","https://www.facebook.com/acmeplumbing","https://twitter.com/share"],
 "contactPoint":{"@type":"ContactPoint","email":"support@acme.example","telephone":"020 7946 0001"}}
</script></head><body><h1>Acme</h1></body></html>"""


def test_extract_structured_reads_jsonld():
    s = extract_structured(LD)
    assert s["emails"] == ["info@acme.example", "support@acme.example"]
    assert s["phones"] == ["+44 20 7946 0000", "020 7946 0001"]
    assert s["socials"] == {"instagram": "https://instagram.com/acmeplumb", "facebook": "https://facebook.com/acmeplumbing"}


def test_extract_structured_ignores_broken_json_and_pages_without_ld():
    assert extract_structured("<html><body>no data</body></html>") == {"emails": [], "phones": [], "socials": {}}
    assert extract_structured('<script type="application/ld+json">{not json</script>') == {"emails": [], "phones": [], "socials": {}}


# ── contact page ranking ─────────────────────────────────────────────────────
NAV = """<html><body><nav>
<a href="/about-us">About us</a><a href="/team">Meet the team</a><a href="/services">Services</a>
<a href="/find-us">Find us</a><a href="/contact-us">Contact</a><a href="https://other.example/contact">Partner</a>
</nav></body></html>"""


def test_contact_links_ranked_contact_first():
    links = contact_page_links(NAV, "https://acme.example/")
    assert links[0] == "https://acme.example/contact-us"
    assert links[1:3] == ["https://acme.example/about-us", "https://acme.example/team"]
    assert "https://acme.example/find-us" in links
    assert all("other.example" not in u for u in links)
    assert len(links) <= 4
