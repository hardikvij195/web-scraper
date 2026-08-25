"""Proxy rotation (W15) — pure logic, no network.

Pins: `ENRICH_PROXIES` parsing, round-robin order, quarantine after N consecutive failures and
re-admit after the cooldown (injected clock), credential redaction everywhere a proxy is
named, and the retry ladder rules in `enrich._with_proxies` (direct first unless
ENRICH_PROXY_FIRST; one rotation on a proxy-blamed failure; a proxy failure never becomes the
verdict on the site).
"""
import asyncio

import pytest

from webscraper import enrich, proxies
from webscraper.enrich import Fetched, _with_proxies, is_block
from webscraper.proxies import ProxyPool, parse_proxy_list, proxy_arg, redact

P1, P2, P3 = "http://u:p@a.test:1", "http://b.test:2", "socks5://c.test:3"


# ── parsing ──────────────────────────────────────────────────────────────────────────
def test_parse_comma_newline_and_whitespace():
    raw = "http://u:p@a.test:1, b.test:2\nsocks5://c.test:3\r\n  \n"
    assert parse_proxy_list(raw) == [P1, P2, P3]


def test_parse_defaults_scheme_dedupes_and_skips_junk():
    assert parse_proxy_list("a.test:1,http://a.test:1,,'b.test:2',http://") == \
        ["http://a.test:1", "http://b.test:2"]
    assert parse_proxy_list(None) == []
    assert parse_proxy_list("") == []


# ── redaction ────────────────────────────────────────────────────────────────────────
def test_redact_never_leaks_credentials():
    assert redact("http://user:s3cret@gw.test:7777") == "gw.test:7777"
    assert redact("gw.test:7777") == "gw.test:7777"
    assert redact("http://gw.test") == "gw.test"
    assert redact(None) is None
    assert "s3cret" not in (redact("user:s3cret@") or "")


def test_proxy_arg_splits_credentials_and_browser_helper_delegates(monkeypatch):
    assert proxy_arg("http://u:p@gw.test:7777") == {"server": "http://gw.test:7777",
                                                    "username": "u", "password": "p"}
    assert proxy_arg(None) is None
    from webscraper import browser_fetch as bf
    monkeypatch.setattr(bf.settings, "enrich_proxy", "http://x:y@legacy.test:1")
    assert bf._proxy_arg() == {"server": "http://legacy.test:1", "username": "x", "password": "y"}
    # An explicit pick beats the ENRICH_PROXY default; "" forces direct even with it set.
    assert bf._proxy_arg("http://pick.test:2") == {"server": "http://pick.test:2"}
    assert bf._proxy_arg("") is None


def test_fetched_tag_is_redacted():
    assert Fetched(proxy=P1).tag("tls") == "tls@a.test:1"
    assert Fetched(proxy=P1).tag("http_403") == "http_403@a.test:1"
    assert Fetched().tag("httpx") == "httpx"
    assert Fetched(proxy=P1).tag(None) is None


def test_is_block_ignores_proxy_tag():
    assert is_block("http_403@a.test:1")
    assert is_block("timeout@a.test:1")
    assert not is_block("http_404@a.test:1")
    assert not is_block("proxy_connect")


# ── pool: rotation, quarantine, re-admit ─────────────────────────────────────────────
class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_round_robin_and_success_resets_streak():
    clk = Clock()
    pool = ProxyPool([P1, P2, P3], max_failures=2, cooldown_sec=60, clock=clk)
    assert [pool.next() for _ in range(4)] == [P1, P2, P3, P1]
    pool.failure(P1)
    pool.success(P1)
    assert pool.failures(P1) == 0
    assert len(pool) == 3 and bool(pool)


def test_quarantine_after_n_consecutive_failures_and_readmit_after_cooldown():
    clk = Clock()
    pool = ProxyPool([P1, P2], max_failures=2, cooldown_sec=60, clock=clk)
    assert pool.failure(P1, "proxy_407") is False
    assert pool.failure(P1, "proxy_407") is True          # benched on the 2nd
    assert pool.is_benched(P1)
    assert [pool.next() for _ in range(3)] == [P2, P2, P2]  # only P2 is offered
    clk.t += 59
    assert pool.is_benched(P1)
    clk.t += 1                                            # cooldown elapsed
    assert not pool.is_benched(P1)
    assert pool.failures(P1) == 0                         # clean streak on re-admit
    assert P1 in [pool.next(), pool.next()]


def test_all_benched_returns_none_and_unknown_proxy_ignored():
    clk = Clock()
    pool = ProxyPool([P1], max_failures=1, cooldown_sec=10, clock=clk)
    pool.failure(P1)
    assert pool.next() is None
    assert pool.failure("http://nope.test:9") is False
    clk.t += 10
    assert pool.next() == P1


def test_empty_pool_is_falsy():
    pool = ProxyPool([])
    assert not pool and pool.next() is None


def test_get_pool_precedence(monkeypatch):
    # ENRICH_PROXIES builds a pool; a lone ENRICH_PROXY does NOT (W13 path untouched).
    proxies.reset_pool()
    from webscraper.config import settings
    monkeypatch.setattr(settings, "enrich_proxies", [])
    monkeypatch.setattr(settings, "enrich_proxy", "http://legacy.test:1")
    assert proxies.get_pool() is None
    proxies.reset_pool()
    monkeypatch.setattr(settings, "enrich_proxies", [P1, P2])
    pool = proxies.get_pool()
    assert pool is not None and pool.urls == [P1, P2]
    proxies.reset_pool()


# ── ladder rules ─────────────────────────────────────────────────────────────────────
def _runner(script: dict[str | None, list[Fetched]]):
    """attempt(proxy) that pops the next scripted result for that proxy and records calls."""
    calls: list[str | None] = []

    async def attempt(p: str | None) -> Fetched:
        calls.append(p)
        return script[p].pop(0)
    return attempt, calls


def test_direct_first_uses_proxy_only_when_blocked():
    pool = ProxyPool([P1, P2], clock=Clock())
    ok = lambda: Fetched(html="<html>site</html>")  # noqa: E731 — fresh per attempt (results are mutated)
    attempt, calls = _runner({None: [ok()]})
    got = asyncio.run(_with_proxies(attempt, pool, proxy_first=False))
    assert got.html and got.proxy is None and calls == [None]
    # A 404 is not a block: no proxy is spent on it.
    attempt, calls = _runner({None: [Fetched(error="http_404")]})
    got = asyncio.run(_with_proxies(attempt, pool, proxy_first=False))
    assert got.error == "http_404" and calls == [None]
    # A 403 IS: one proxied attempt, which succeeds and is credited to the proxy.
    attempt, calls = _runner({None: [Fetched(error="http_403")], P1: [ok()]})
    got = asyncio.run(_with_proxies(attempt, pool, proxy_first=False))
    assert got.html and got.proxy == P1 and calls == [None, P1]
    assert pool.failures(P1) == 0


def test_proxy_error_rotates_once_then_returns_site_verdict():
    clk = Clock()
    pool = ProxyPool([P1, P2], max_failures=3, clock=clk)
    ok = lambda: Fetched(html="<html>site</html>")  # noqa: E731 — fresh per attempt (results are mutated)
    # P1 cannot be reached → retried once via P2, which reads the page.
    attempt, calls = _runner({None: [Fetched(error="http_403")],
                              P1: [Fetched(error="proxy_connect")], P2: [ok()]})
    got = asyncio.run(_with_proxies(attempt, pool, proxy_first=False))
    assert got.html and got.proxy == P2 and calls == [None, P1, P2]
    assert pool.failures(P1) == 1 and pool.failures(P2) == 0
    # Both proxies fail on THEMSELVES: the tier's verdict is the site's own 403 (so the next
    # tier still runs), and both failures count.
    attempt, calls = _runner({None: [Fetched(error="http_403")],
                              P1: [Fetched(error="proxy_407")], P2: [Fetched(error="proxy_407")]})
    got = asyncio.run(_with_proxies(attempt, pool, proxy_first=False))
    assert got.error == "http_403" and got.proxy is None
    assert calls[0] is None and set(calls[1:]) == {P1, P2}
    assert pool.failures(P1) == 2 and pool.failures(P2) == 1


def test_site_403_via_proxy_is_reported_with_proxy_but_not_counted():
    pool = ProxyPool([P1], clock=Clock())
    attempt, calls = _runner({None: [Fetched(error="http_403")], P1: [Fetched(error="http_403")]})
    got = asyncio.run(_with_proxies(attempt, pool, proxy_first=False))
    assert got.error == "http_403" and got.proxy == P1
    assert got.tag(got.error) == "http_403@a.test:1"
    assert pool.failures(P1) == 0        # ambiguous: the site may block everyone


def test_proxy_first_order_and_known_good_control():
    pool = ProxyPool([P1, P2], max_failures=5, clock=Clock())
    ok = lambda: Fetched(html="<html>site</html>")  # noqa: E731 — fresh per attempt (results are mutated)
    attempt, calls = _runner({P1: [ok()]})
    got = asyncio.run(_with_proxies(attempt, pool, proxy_first=True))
    assert got.proxy == P1 and calls == [P1]
    # Proxy 403 + direct 200 = the site is fine and this exit IP is burned: counted.
    attempt, calls = _runner({P2: [Fetched(error="http_403")], None: [ok()]})
    got = asyncio.run(_with_proxies(attempt, pool, proxy_first=True))
    assert got.html and got.proxy is None and calls == [P2, None]
    assert pool.failures(P2) == 1
    # Proxy unreachable → rotate once → direct as the last resort.
    attempt, calls = _runner({P1: [Fetched(error="proxy_connect")], P2: [Fetched(error="proxy_connect")],
                              None: [ok()]})
    got = asyncio.run(_with_proxies(attempt, pool, proxy_first=True))
    assert got.html and calls == [P1, P2, None]


def test_no_pool_is_a_single_direct_attempt():
    attempt, calls = _runner({None: [Fetched(error="http_403")]})
    got = asyncio.run(_with_proxies(attempt, None, proxy_first=True))
    assert got.error == "http_403" and calls == [None]


# ── crawl_site reporting end-to-end (fetch tiers stubbed) ────────────────────────────
def test_crawl_site_surfaces_proxy_in_via_and_error(monkeypatch):
    pool = ProxyPool([P1], clock=Clock())
    monkeypatch.setattr(enrich, "get_pool", lambda: pool)
    monkeypatch.setattr(enrich.settings, "enrich_proxy_first", False)

    async def home(client, website, proxy=None):
        return Fetched(error="http_403", url="https://x.test", proxy=proxy)

    async def tls(url, proxy=None):
        return ("<html><body>hello@x.test</body></html>", None) if proxy == P1 else (None, "http_403")
    monkeypatch.setattr(enrich, "_fetch_home", home)
    monkeypatch.setattr(enrich, "impersonate_fetch_ex", tls)
    c, reason = asyncio.run(enrich.crawl_site(None, "x.test"))
    assert reason is None and c.via == "tls@a.test:1" and "hello@x.test" in c.emails

    async def tls_fail(url, proxy=None):
        return None, "http_403"
    monkeypatch.setattr(enrich, "impersonate_fetch_ex", tls_fail)

    async def browser(url):
        return None, P1
    c, reason = asyncio.run(enrich.crawl_site(None, "x.test", browser))
    assert c.pages_fetched == 0 and reason == "http_403@a.test:1"

    async def browser_ok(url):
        return "<html><body>hi@x.test</body></html>", P1
    c, reason = asyncio.run(enrich.crawl_site(None, "x.test", browser_ok))
    assert reason is None and c.via == "browser@a.test:1"


def test_impersonate_error_classification():
    from webscraper.impersonate_fetch import _classify_exception
    assert _classify_exception(Exception("Failed to connect to proxy"), True) == "proxy_connect"
    assert _classify_exception(Exception("Failed to connect to host"), False) == "network"
    assert _classify_exception(Exception("Operation timed out"), False) == "timeout"


@pytest.mark.parametrize("raw", ["http://u:p@a.test:1", "a.test:1"])
def test_parse_roundtrips_redact(raw):
    assert redact(parse_proxy_list(raw)[0]) == "a.test:1"
