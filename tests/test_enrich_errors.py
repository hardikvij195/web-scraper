"""Why a crawl produced nothing — the classification only, no network.

Live CRM job #6 (Greater London) enriched 33 leads and left 9 at `enrich_status='failed'`
with no reason recorded anywhere. Reproducing them by hand showed 8 were WAF/Cloudflare
403s (a browser gets those pages) and 1 was a domain that no longer resolves (nothing gets
that page). These tests pin the mapping that now writes `places.enrich_error`, so the next
9 failures explain themselves."""
import httpx

from webscraper.enrich import crawl_error, http_error, is_block, transport_error
from webscraper.store import plus


def test_http_status_to_error():
    assert http_error(403) == "http_403"      # lookers.co.uk, hrowen.co.uk, carluv.co.uk …
    assert http_error(429) == "http_429"
    assert http_error(503) == "http_503"
    assert http_error(404) == "http_404"


def test_is_block():
    # Only these earn the ~5 s browser retry; everything else stays failed.
    assert is_block("http_403")
    assert is_block("http_429")
    assert is_block("http_503")
    assert not is_block("http_404")           # page is gone, a browser won't conjure it
    assert not is_block("dns")
    assert not is_block(None)


def test_dns_error():
    # The real job-6 case: https://atypesourcing.com/ -> ConnectError getaddrinfo failed.
    assert transport_error(httpx.ConnectError("[Errno 11001] getaddrinfo failed")) == "dns"
    assert transport_error(httpx.ConnectError("Name or service not known")) == "dns"


def test_timeout_error():
    assert transport_error(httpx.ConnectTimeout("timed out")) == "timeout"
    assert transport_error(httpx.ReadTimeout("timed out")) == "timeout"


def test_other_transport_errors_are_network():
    # A TLS/connection-refused failure is not DNS — mislabelling it would say "dead domain"
    # about a site that is merely down right now.
    assert transport_error(httpx.ConnectError("[Errno 111] Connection refused")) == "network"
    assert transport_error(httpx.RemoteProtocolError("server disconnected")) == "network"


def test_crawl_error_reasons():
    assert crawl_error(0, "http_403") == "http_403"
    assert crawl_error(0, "dns") == "dns"
    assert crawl_error(0, "timeout") == "timeout"
    assert crawl_error(0, "non_html") == "non_html"
    assert crawl_error(0, None) == "no_pages"          # 200 and still nothing to show
    # Anything fetched clears the reason, so a re-enrich that works drops the stale 403.
    assert crawl_error(1, "http_403") is None
    assert crawl_error(3, None) is None


def test_plus_formats_e164():
    assert plus("447700900123") == "+447700900123"
    assert plus("+447700900123") == "+447700900123"    # idempotent
    assert plus("+91 98765-43210") == "+919876543210"
    assert plus(None) is None
    assert plus("") is None
    assert plus("n/a") is None
