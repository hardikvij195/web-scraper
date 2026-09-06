"""Website enrichment: fetch a business site (home + contact/about pages), pull emails,
social profiles and a WhatsApp number. Plain HTTP (httpx) — fast, no browser, and it
hits each business's own site, never Google, so it needs no pacing.

When httpx cannot get the page, the reason is recorded (`places.enrich_error`) and, for the
block-shaped ones only, the site is retried once through a real Chromium — see
`browser_fetch.py` for why and how little that path is allowed to cost."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from webscraper.config import settings
from webscraper.extractors import (
    contact_page_links, extract_emails, extract_phones, extract_socials, extract_structured, extract_whatsapp,
    is_js_shell, is_probably_mobile, normalise_wa, region_of_phone,
)
from webscraper.config import settings
from webscraper.impersonate_fetch import impersonate_fetch_ex
from webscraper.proxies import PROXY_ERRORS, ProxyPool, get_pool, redact
from webscraper.models import Contacts
from webscraper.store import Store, now_iso, plus

log = logging.getLogger("webscraper.enrich")

#: W22: a FULL, internally consistent Chrome 150 desktop header set, hand-written to match
#: curl_cffi's `chrome` alias (chrome150 = the macOS UA below) so the httpx tier and the TLS
#: tier present the same identity. Cloudflare scores the whole set: a Chrome UA with no
#: `sec-ch-ua` / `Sec-Fetch-*` is a bot tell in itself. `Accept-Encoding` lists only what
#: httpx can actually decode here (br needs `brotli`, zstd needs `zstandard`) — advertising
#: an encoding we cannot decode would turn a 200 into garbage.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/150.0.0.0 Safari/537.36")
CHROME_MAJOR = "150"
SEC_CH_UA = f'"Not;A=Brand";v="8", "Chromium";v="{CHROME_MAJOR}", "Google Chrome";v="{CHROME_MAJOR}"'
SEC_CH_UA_PLATFORM = '"macOS"'
ACCEPT_LANGUAGE = "en-GB,en;q=0.9"
REFERER = "https://www.google.com/"


def _accept_encoding() -> str:
    encs = ["gzip", "deflate"]
    for enc, mod in (("br", "brotli"), ("zstd", "zstandard")):
        try:
            __import__(mod)
            encs.append(enc)
        except Exception:  # noqa: BLE001 — optional decoder absent: do not advertise it
            pass
    return ", ".join(encs)


HEADERS = {
    "User-Agent": UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
               "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"),
    "Accept-Language": ACCEPT_LANGUAGE,
    "Accept-Encoding": _accept_encoding(),
    "sec-ch-ua": SEC_CH_UA,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "Upgrade-Insecure-Requests": "1",
    "Referer": REFERER,
}
MAX_BYTES = 1_500_000

#: Statuses that mean "a bot filter turned us away", not "this site has no page". These are
#: the only ones worth spending a browser on: 403 = WAF/Cloudflare block, 429 = rate limit,
#: 503 = the Cloudflare interstitial while it decides. Job #6 (Greater London) ended with 9
#: `failed` leads and 8 of them were exactly this.
BLOCK_CODES = (403, 429, 503)


@dataclass(slots=True)
class Fetched:
    """One page fetch attempt: the HTML when it worked, and WHY when it did not.

    `_fetch` used to return None for every kind of failure, so `enrich_status='failed'`
    carried no reason at all. Reproducing job #6's 9 failures by hand was the only way to
    learn that 8 were bot blocks (retryable in a browser) and 1 was a domain that no longer
    resolves (dead forever). That distinction now reaches the DB instead of a human."""

    html: str | None = None
    error: str | None = None
    #: W15: the proxy URL this attempt went through (None = direct). Redacted before it is
    #: ever logged or stored — see `tag()`.
    proxy: str | None = None
    #: The URL that was actually fetched (the http→https switch may change it).
    url: str | None = None

    def tag(self, label: str | None) -> str | None:
        """`label` suffixed with '@host:port' when a proxy was used ('tls@gw.test:7777'), so
        the job log shows WHICH proxy read (or failed) a site; credentials never appear."""
        if label is None:
            return None
        return f"{label}@{redact(self.proxy)}" if self.proxy else label


def http_error(status_code: int) -> str:
    """'http_403', 'http_404', … — the raw code is kept because the codes mean different
    things to us: 403/429/503 earn a browser retry, 404/410 mean the page is simply gone."""
    return f"http_{status_code}"


def is_block(error: str | None) -> bool:
    """True for the failures a real browser has a chance against.

    `timeout` is in the list on evidence, not on principle: job #7 finished with 11
    timeouts against 15 outright 403s, and a plain httpx timeout is often a host that
    is slow, JS-gated or quietly throttling a non-browser client rather than a site
    that is genuinely down — all cases a real browser page load can still win.

    Deliberately NOT here: `dns` (the domain does not resolve — nothing to retry with)
    and 404/400/500 (the server answered, and answered no).
    """
    if error and "@" in error:
        error = error.split("@", 1)[0]          # 'http_403@gw:7777' — the proxy tag (W15)
    if error == "cf_deny":
        # W56: Cloudflare's IP/ASN/country rule (Error 1020 / 1015) — a static deny with no
        # challenge to solve. Every tier on this same IP gets the same page, so escalating
        # only burns ~5 s of browser per site. A proxy is the one thing that can change it
        # (`_with_proxies` treats it as a block when a pool is configured).
        return False
    if error and (error.startswith("cf_") or error in ("blocked", "recaptcha")):
        # W22: a Cloudflare wall classified by the browser tier (cf_non_interactive /
        # cf_managed / cf_interactive / cf_embedded) or a 200 whose body was an
        # interstitial — a block by definition, and what the next tier exists for.
        return True
    return error == "timeout" or error in tuple(http_error(c) for c in BLOCK_CODES)


def is_ip_deny(error: str | None) -> bool:
    """A Cloudflare 1020/1015 deny: only a different exit IP can help (W56)."""
    return (error or "").split("@", 1)[0] == "cf_deny"


#: W56: the audit's 'network' bucket (435 of 1,708 failures) hid three different things.
#: These markers split it so the CRM can say which — and so `_fetch_home` knows which ones
#: are host-level (worth the www./non-www variant) rather than a verdict on the page.
_TLS_MARKERS = ("ssl", "tls", "certificate", "handshake", "wrong_version_number",
                "unexpected_eof", "sslv3", "alert internal error")
_RESET_MARKERS = ("connection reset", "econnreset", "10054", "remoteprotocolerror",
                  "server disconnected", "connection closed", "peer closed")
_REFUSED_MARKERS = ("connection refused", "econnrefused", "10061", "actively refused")
#: Failures that are about the HOST, not the page — the only ones where trying the other
#: hostname spelling (www. ⇄ bare) can change the answer.
HOST_ERRORS = ("dns", "tls", "reset", "refused", "timeout")

#: getaddrinfo's message differs per platform; all of them mean the same dead domain.
_DNS_MARKERS = ("getaddrinfo", "name or service not known", "nodename nor servname",
                "temporary failure in name resolution", "no address associated",
                "name does not resolve")


def transport_error(e: Exception) -> str:
    """Map an httpx-level failure onto a stored reason.

    'dns' is worth separating out because it is terminal: `atypesourcing.com` (job #6) does
    not resolve, so no retry, browser or otherwise, will ever help — that lead's website is
    dead and only its phone is left. 'timeout' and the 'network' catch-all can both be
    transient, so re-enriching those is worth queueing. W56 splits 'network' further into
    'tls' (handshake / certificate), 'reset' (the host dropped us mid-connection — often a
    soft block) and 'refused' (nothing listening on that port)."""
    if isinstance(e, httpx.TimeoutException):
        return "timeout"
    msg = str(e).lower()
    if any(m in msg for m in _DNS_MARKERS):
        return "dns"
    if any(m in msg for m in _TLS_MARKERS):
        return "tls"
    if isinstance(e, httpx.RemoteProtocolError) or any(m in msg for m in _RESET_MARKERS):
        return "reset"
    if any(m in msg for m in _REFUSED_MARKERS):
        return "refused"
    return "network"


#: W56: Cloudflare's static deny pages. 1020 = an IP/ASN/country firewall rule, 1015 = its
#: rate limiter. Both arrive as a 403/429 whose BODY names the code; neither offers a
#: challenge, so `http_403` (which earns the whole ladder) is the wrong verdict for them.
_CF_DENY_RE = re.compile(r"error code:?\s*10(?:15|20)\b|error 10(?:15|20)\b|"
                         r"<title>\s*access denied\s*\|.*cloudflare", re.I | re.S)
#: 429 back-off: one pause then one retry, honouring Retry-After when it is short. A
#: straight retry at the same pace is exactly what the audit's 429s got, and it is a 429 twice.
RATE_LIMIT_WAIT_SEC = 8.0
RATE_LIMIT_MAX_WAIT_SEC = 20.0


def classify_deny(status_code: int, body: str | None) -> str:
    """The stored reason for a 4xx: 'cf_deny' when the body is Cloudflare's 1020/1015 page,
    else the plain 'http_<code>'."""
    if status_code in (403, 429, 503) and body and _CF_DENY_RE.search(body[:6000]):
        return "cf_deny"
    return http_error(status_code)


def crawl_error(pages_fetched: int, reason: str | None) -> str | None:
    """The reason to store for a finished crawl.

    None once anything at all was fetched — a re-enrich that finally works has to CLEAR the
    stale reason rather than leave last week's 403 sitting on a lead that is fine now.
    'no_pages' covers the odd case where the site answered 200 and still gave us nothing."""
    if pages_fetched > 0:
        return None
    return reason or "no_pages"


async def _fetch_ex(client: httpx.AsyncClient, url: str, retries: int = 1,
                    proxy: str | None = None) -> Fetched:
    """GET a page; one retry on transport errors (slow shared hosts time out sporadically).
    Never raises — the failure comes back as `Fetched.error` so the caller can store it.

    `proxy` (W15) routes this ONE fetch through a proxy: httpx binds the proxy per client, so
    a short-lived client that borrows `client`'s headers/timeout is opened for it. A 407 or
    a failure to reach the gateway is reported as a proxy error, not a site error."""
    if proxy:
        try:
            async with httpx.AsyncClient(headers=client.headers, follow_redirects=True,
                                         timeout=client.timeout, verify=False,
                                         proxy=proxy) as pc:
                got = await _fetch_ex(pc, url, retries)
        except Exception as e:                       # noqa: BLE001 — bad proxy URL etc.
            log.debug("proxied fetch could not start %s via %s: %s", url, redact(proxy), e)
            got = Fetched(error="proxy_connect")
        got.proxy = proxy
        return got
    backed_off = False
    attempt, budget = 0, retries + 1
    while attempt < budget:
        attempt += 1
        try:
            r = await client.get(url)
            ctype = r.headers.get("content-type", "")
            if r.status_code == 407:
                return Fetched(error="proxy_407")
            if r.status_code == 429 and not backed_off:
                # W56: the site is pacing us, not refusing us — wait once, then one more GET
                # (on top of the transport-retry budget, which this does not consume).
                backed_off = True
                budget += 1
                try:
                    wait = min(float(r.headers.get("retry-after") or RATE_LIMIT_WAIT_SEC), RATE_LIMIT_MAX_WAIT_SEC)
                except ValueError:
                    wait = RATE_LIMIT_WAIT_SEC
                log.debug("429 from %s — backing off %.0fs", url, wait)
                await asyncio.sleep(wait)
                continue
            if r.status_code >= 400:
                return Fetched(error=classify_deny(r.status_code, r.text[:6000] if "html" in ctype else None))
            if ctype and "html" not in ctype and "xml" not in ctype:
                # A PDF/image/JSON "website" — a real response, just nothing to parse.
                return Fetched(error="non_html")
            return Fetched(html=r.text[:MAX_BYTES])
        except httpx.ProxyError as e:
            log.debug("proxy failed for %s: %s", url, e)
            return Fetched(error="proxy_connect")
        except httpx.TransportError as e:
            log.debug("fetch failed %s (attempt %d): %s", url, attempt, e)
            if attempt < budget:
                await asyncio.sleep(1.5)
                continue
            return Fetched(error=transport_error(e))
        except httpx.HTTPError as e:
            log.debug("fetch failed %s: %s", url, e)
            return Fetched(error=transport_error(e))
        except (UnicodeDecodeError, ValueError) as e:
            log.debug("fetch failed %s: %s", url, e)
            return Fetched(error="non_html")
    return Fetched(error="no_pages")


async def _fetch(client: httpx.AsyncClient, url: str, retries: int = 1) -> str | None:
    """HTML only. Kept at the module's original signature because `research.py` imports it
    and only ever wanted the text; new callers use `_fetch_ex` and store the reason."""
    return (await _fetch_ex(client, url, retries)).html


def _merge(into: Contacts, html: str, region: str = "IN") -> None:
    for e in extract_emails(html):
        if e not in into.emails:
            into.emails.append(e)
    for net, url in extract_socials(html).items():
        if getattr(into, net) is None:
            setattr(into, net, url)
    if into.whatsapp_number is None:
        into.whatsapp_number = extract_whatsapp(html)
    # W26: every number the site lists, so WhatsApp verification can check them all —
    # not just the one Google Maps shows. Capped inside extract_phones; deduped here.
    if len(into.phones) < 6:
        for p in extract_phones(html, region):
            if p not in into.phones:
                into.phones.append(p)
    # W56: JSON-LD `LocalBusiness` / `Organization` blocks carry email / telephone / sameAs
    # that never appear as visible text or <a> links on builder sites (Wix, Squarespace,
    # Shopify themes) — 1,852 "done" leads in the audit had nothing extracted at all.
    structured = extract_structured(html)
    for e in structured["emails"]:
        if e not in into.emails:
            into.emails.append(e)
    for net, url in structured["socials"].items():
        if getattr(into, net) is None:
            setattr(into, net, url)
    if len(into.phones) < 6:
        for p in extract_phones(" ".join(f'href="tel:{t}"' for t in structured["phones"]), region):
            if p not in into.phones:
                into.phones.append(p)


def _www_variant(url: str) -> str | None:
    """'https://www.x.co.uk/' ⇄ 'https://x.co.uk/', or None when the host has no simple
    variant (an IP, a deeper subdomain, a bare TLD)."""
    try:
        s = urlsplit(url)
    except ValueError:
        return None
    host = s.netloc
    if not host or host.replace(".", "").isdigit():
        return None
    if host.lower().startswith("www."):
        alt = host[4:]
    elif host.count(".") <= 2:              # x.co.uk / x.com — never  a.b.x.com
        alt = "www." + host
    else:
        return None
    return urlunsplit((s.scheme, alt, s.path, s.query, s.fragment))


async def _fetch_home(client: httpx.AsyncClient, website: str,
                      proxy: str | None = None) -> Fetched:
    """The httpx tier: home page over http, then https when http gave nothing. The URL that
    ends up in `Fetched.url` is the one the slower tiers should be pointed at.

    W56: when BOTH schemes fail at the host level (dns / tls / reset / refused / timeout)
    the other hostname spelling is tried once — 661 of the audit's failures were 'dns', and
    a share of those are Maps listings with `www.` on a zone that only publishes the bare
    name (or the reverse), not dead sites."""
    url = website if "://" in website else "http://" + website
    got = await _fetch_ex(client, url, proxy=proxy)
    if got.html is None and url.startswith("http://"):
        url_https = "https://" + url[7:]
        alt = await _fetch_ex(client, url_https, proxy=proxy)
        # Switch to https when it worked, and also when it is the half that got blocked —
        # that is the URL a browser retry should be pointed at (a plain-http fetch of an
        # https-only host fails for boring reasons that say nothing about the real site).
        if alt.html is not None or is_block(alt.error):
            url, got = url_https, alt
        elif got.error is None:
            got = alt
    if got.html is None and got.error in HOST_ERRORS:
        variant = _www_variant("https://" + url.split("://", 1)[1])
        if variant:
            v = await _fetch_ex(client, variant, proxy=proxy)
            if v.html is None and v.error in HOST_ERRORS and got.error == "dns":
                v = await _fetch_ex(client, "http://" + variant.split("://", 1)[1], proxy=proxy)
            if v.html is not None or is_block(v.error):
                log.info("host variant rescued %s (%s said %s)", variant, url, got.error)
                url, got = variant, v
    got.url = url
    return got


async def _noop_fetch(got: Fetched) -> Fetched:
    """The already-known direct verdict, for a proxy-only pass (W56 cf_deny)."""
    return got


async def _with_proxies(attempt: Callable[[str | None], Awaitable[Fetched]],
                        pool: ProxyPool | None, proxy_first: bool) -> Fetched:
    """Run ONE tier of the ladder under the W15 proxy rules.

    Direct first (default): the own-IP attempt, and only if it was BLOCKED one attempt via
    the pool's next proxy; a proxy-blamed failure there (407 / cannot reach the gateway) earns
    one retry with the next proxy before the tier gives up. `ENRICH_PROXY_FIRST=1` flips the
    order: proxy (+ one rotation on a proxy error), then direct.

    A proxy-blamed failure never becomes the tier's verdict on the SITE: the result handed
    back is the site's own error so the next tier still runs. Successes and proxy failures
    feed the pool's counters; a site 403 does not (the exit IP MAY be burned, but the site
    may block everyone — that is not evidence against the proxy)."""
    if not pool:
        return await attempt(None)

    async def via_pool() -> Fetched | None:
        p = pool.next()
        if p is None:
            return None                      # every proxy benched — behave as if none
        got = await attempt(p)
        got.proxy = p
        if got.html is not None:
            pool.success(p)
            return got
        if got.error in PROXY_ERRORS:
            pool.failure(p, got.error)
            p2 = pool.next()
            if p2 and p2 != p:
                got2 = await attempt(p2)
                got2.proxy = p2
                if got2.html is not None:
                    pool.success(p2)
                elif got2.error in PROXY_ERRORS:
                    pool.failure(p2, got2.error)
                return got2
        return got

    if proxy_first:
        got = await via_pool()
        if got is not None and got.html is not None:
            return got
        direct = await attempt(None)
        if direct.html is not None and got is not None and (is_block(got.error) or is_ip_deny(got.error)):
            # The site answered the own IP but blocked the proxy's: a 403 on a known-good
            # page is the one case a site block IS evidence against the proxy.
            pool.failure(got.proxy or "", f"{got.error} on known-good page")
        if direct.html is not None or got is None or got.error in PROXY_ERRORS:
            return direct
        # Both failed on the site itself: keep the proxied verdict (it names the proxy)
        # unless only the direct one is a block the next tier can still act on.
        return got if is_block(got.error) or not is_block(direct.error) else direct
    direct = await attempt(None)
    if direct.html is not None or not (is_block(direct.error) or is_ip_deny(direct.error)):
        return direct
    got = await via_pool()
    if got is None or got.error in PROXY_ERRORS:
        return direct
    return got


async def crawl_site(client: httpx.AsyncClient, website: str,
                     browser_retry: Callable[[str], Awaitable[Any]] | None = None,
                     camoufox_retry: Callable[[str], Awaitable[tuple[str | None, str | None]]] | None = None,
                     region: str | None = None,
                     ) -> tuple[Contacts, str | None]:
    """Home page + up to 4 contact/about pages on the same domain.

    Returns the contacts plus the reason nothing was fetched (None when something was).
    `browser_retry`, when given, is the slow path from `browser_fetch.py`: it is offered the
    home page URL once, and only when httpx was blocked rather than merely refused. It may
    return the HTML, an `(html, proxy_url)` pair so the proxy shows in the report, or a
    `(html, proxy_url, error)` triple where `error` names the Cloudflare wall the browser
    saw (`cf_managed` …, W22). `camoufox_retry` (W22, `ENRICH_BROWSER_CAMOUFOX=1`) is the
    last tier after that, returning `(html, error)`.

    Fetch ladder: httpx → curl_cffi TLS impersonation → real browser, each tier under the W15
    proxy rules of `_with_proxies` (direct first unless ENRICH_PROXY_FIRST). The tier and
    the proxy that finally read the page land in `Contacts.via` ('tls@gw.test:7777'); on a
    total failure the returned reason carries the same tag ('http_403@gw.test:7777')."""
    c = Contacts()
    pool = get_pool()
    proxy_first = bool(settings.enrich_proxy_first)
    got = await _with_proxies(lambda p: _fetch_home(client, website, p), pool, proxy_first)
    url = got.url or (website if "://" in website else "http://" + website)
    via = got.tag("httpx") if got.html is not None else None
    # W56: a JS-only shell (`<div id="root"></div>` + a bundle, no text) is a 200 that still
    # tells us nothing — it used to be stored as 'done' with no contacts. Only the browser
    # renders it, so it goes straight there; the shell stays as the fallback if that fails.
    if got.html is not None and browser_retry is not None and is_js_shell(got.html):
        res = await browser_retry(url)
        html = res[0] if isinstance(res, tuple) else res
        if html and not is_js_shell(html):
            log.info("browser rendered JS-only shell %s", url)
            got, via = Fetched(html=html, url=url), "browser"
    if got.html is None and is_ip_deny(got.error):
        # W56: Cloudflare 1020/1015 — same IP, same answer; only a proxy changes it.
        if pool:
            got = await _with_proxies(lambda p: _fetch_home(client, website, p) if p else _noop_fetch(got), pool, True)
            via = got.tag("httpx") if got.html is not None else None
        else:
            log.debug("cloudflare deny (%s) for %s — no proxy pool, not escalating", got.error, url)
    # TLS-impersonation retry: cheaper than the browser and beats the fingerprint-403s that
    # make up most blocks (see impersonate_fetch). Sits BEFORE the browser so the ~5 s slow
    # path is only paid for the JS challenges curl_cffi cannot pass. Self-gating and optional
    # — a no-op if disabled or curl_cffi is absent — so this is a pure add to the fall-through.
    if got.html is None and is_block(got.error):
        async def tls(p: str | None) -> Fetched:
            # "" = force direct: with a pool configured, ENRICH_PROXIES supersedes the
            # single ENRICH_PROXY default that impersonate_fetch would otherwise apply.
            html, err = await impersonate_fetch_ex(url, proxy=(p if p else ("" if pool else None)))
            return Fetched(html=html, error=err)
        t = await _with_proxies(tls, pool, proxy_first)
        if t.html:
            log.info("tls impersonate rescued %s (httpx said %s)%s", url, got.error,
                     f" via {redact(t.proxy)}" if t.proxy else "")
            got, via = t, t.tag("tls")
    if got.html is None and is_block(got.error) and browser_retry is not None:
        res = await browser_retry(url)
        # (html) | (html, proxy) | (html, proxy, error) — the 3rd is W22's Cloudflare class
        # ('cf_managed' …) so the reason stored says WHICH wall is left, not just 'http_403'.
        html, bproxy, berr = (res if isinstance(res, tuple) and len(res) == 3
                              else (res[0], res[1], None) if isinstance(res, tuple)
                              else (res, None, None))
        if html:
            log.info("browser retry rescued %s (httpx said %s)%s", url, got.error,
                     f" via {redact(bproxy)}" if bproxy else "")
            got = Fetched(html=html, proxy=bproxy)
            via = got.tag("browser")
        elif berr:
            got = Fetched(error=berr, proxy=bproxy, url=url)
    # W22 last tier: Camoufox (a Firefox with a whole-fingerprint rewrite), env-gated OFF.
    # Only after the Chrome tier ALSO failed on a block; one attempt, no proxy rotation.
    if got.html is None and is_block(got.error) and camoufox_retry is not None:
        html, cerr = await camoufox_retry(url)
        if html:
            log.info("camoufox rescued %s (chrome tier said %s)", url, got.error)
            got, via = Fetched(html=html), "camoufox"
        elif cerr and cerr != "off":
            got = Fetched(error=cerr, url=url)
    if got.html is None:
        return c, got.tag(got.error) or "no_pages"
    home = got.html
    c.via = via
    c.pages_fetched = 1
    # `region` = the phone-parsing region for numbers written without a country code
    # (W26 site phones); the lead's own region is passed in by enrich_places.
    region = (region or settings.default_country or "IN").upper()
    _merge(c, home, region)
    if len(home) < 2_000:
        c.thin = True
    # Contact/about pages are only worth the extra requests when the home page left gaps:
    # no email, or no socials at all. (Slow hosts cost ~5 s per page — see onebodyldn.com.)
    socials_found = sum(1 for k in ("instagram", "facebook", "linkedin", "twitter_x") if getattr(c, k))
    if c.emails and socials_found >= 1:
        return c, None
    for link in contact_page_links(home, url)[:2]:
        # Sub-pages get no browser retry: the home page already proved the host answers us,
        # and a per-page browser round trip is exactly the cost this design refuses to pay.
        page = await _fetch(client, link)
        if page:
            c.pages_fetched += 1
            _merge(c, page, region)
        if c.emails and (socials_found or c.instagram or c.facebook):
            break
    return c, None


async def enrich_places(store: Store, rows: list[dict[str, Any]], concurrency: int | None = None,
                        country: str | None = None,
                        on_progress: Callable[[dict[str, Any], str], None] | None = None,
                        should_stop: Callable[[], bool] | None = None,
                        headless: bool | None = None) -> dict[str, int]:
    """Run crawl_site over `rows` (dicts from Store.places) and write results back.
    `should_stop` is checked before each site; remaining rows stay `pending`.

    `headless` controls the browser SLOW PATH's window: None keeps the module default
    (ENRICH_BROWSER_HEADLESS), False forces a visible window. This is how the CRM's
    per-job "Show window" choice reaches the enrichment browser — without it the toggle
    only ever affected Maps discovery, so a re-enrich (enrichment only) ran hidden no
    matter what the user picked."""
    country = country or settings.default_country
    should_stop = should_stop or (lambda: False)
    sem = asyncio.Semaphore(concurrency or settings.enrich_concurrency)
    counts = {"done": 0, "no_website": 0, "failed": 0, "thin": 0}
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
    timeout = httpx.Timeout(15.0, connect=10.0)

    def region_for(r: dict[str, Any]) -> str:
        # Lead's own phone > country guessed from its address > job country.
        return region_of_phone(r.get("phone"), r.get("country") or country)

    def wa_fallback(r: dict[str, Any]) -> tuple[str | None, str]:
        reg = region_for(r)
        # Maps itself showed a WhatsApp button → strongest signal, keep it.
        if r.get("whatsapp_source") == "maps_link" and r.get("whatsapp_number"):
            return normalise_wa(r["whatsapp_number"], reg), "maps_link"
        if is_probably_mobile(r.get("phone"), reg):
            # 'unverified', not the retired 'assumed_mobile' (user directive 2026-08-23:
            # "donot assume any wa link ... verify it"). A plain mobile number is a
            # CANDIDATE for WhatsApp verification and nothing more — the wa_verify lane is
            # what turns it into a claim. Priority is unchanged: maps_link > wa_link >
            # unverified > none.
            return r.get("phone_digits"), "unverified"
        return None, "none"

    async def resolve_short_wa(client: httpx.AsyncClient, r: dict[str, Any]) -> str | None:
        """wa.link/… short links on the Maps panel redirect to wa.me/<number>; follow once."""
        for link in (r.get("raw") or {}).get("maps_wa_links") or []:
            if "wa.link" in link:
                try:
                    resp = await client.get(link)
                    num = extract_whatsapp(str(resp.url)) or extract_whatsapp(resp.text[:20000])
                    if num:
                        return num
                except httpx.HTTPError:
                    continue
        return None

    # The browser slow path. Built on the FIRST site that is actually blocked and then shared
    # by the rest of the run, so a batch where httpx works everywhere never launches Chromium
    # — which is most batches, and the reason enrichment can keep pace with discovery.
    browser: dict[str, Any] = {"fetcher": None, "off": False}
    browser_lock = asyncio.Lock()

    async def browser_retry(url: str) -> tuple[str | None, str | None]:
        async with browser_lock:
            if browser["fetcher"] is None:
                if browser["off"]:
                    return None, None
                try:
                    # Imported here, not at module scope: this keeps Playwright out of the
                    # import graph of every enrich/research run that never needs it.
                    from webscraper.browser_fetch import BROWSER_FALLBACK, BrowserFetcher
                    if not BROWSER_FALLBACK:
                        browser["off"] = True
                        return None, None
                    # W15: a persistent context binds its proxy at launch, so the browser
                    # takes the pool's next proxy once per launch (or runs direct when every
                    # proxy is benched — "" beats the single-ENRICH_PROXY default). Without
                    # a pool, None keeps the W13 ENRICH_PROXY behaviour.
                    pool = get_pool()
                    browser["proxy"] = (pool.next() or "") if pool else None
                    browser["fetcher"] = await asyncio.to_thread(
                        lambda: BrowserFetcher(headless=headless, proxy=browser["proxy"]))
                except Exception as e:                # noqa: BLE001 — no browser, no retry
                    log.warning("browser fallback unavailable (%s) — blocked sites stay failed", e)
                    browser["off"] = True
                    return None, None
        html, err = await asyncio.to_thread(browser["fetcher"].fetch_ex, url)
        return html, (browser.get("proxy") or None), err

    # W22 Camoufox tier — same lazy, once-per-run shape as the Chrome tier; inert unless
    # ENRICH_BROWSER_CAMOUFOX=1, and skipped with one log line when camoufox is not installed.
    camoufox: dict[str, Any] = {"fetcher": None, "off": False}

    async def camoufox_retry(url: str) -> tuple[str | None, str | None]:
        async with browser_lock:
            if camoufox["fetcher"] is None:
                if camoufox["off"]:
                    return None, "off"
                try:
                    from webscraper.camoufox_fetch import CAMOUFOX_ENABLED, CamoufoxFetcher, available
                    if not CAMOUFOX_ENABLED or not available():
                        camoufox["off"] = True
                        return None, "off"
                    camoufox["fetcher"] = await asyncio.to_thread(
                        lambda: CamoufoxFetcher(headless=headless))
                except Exception as e:                # noqa: BLE001 — no camoufox, no tier
                    log.warning("camoufox tier unavailable (%s) — skipped", e)
                    camoufox["off"] = True
                    return None, "off"
        return await asyncio.to_thread(camoufox["fetcher"].fetch_ex, url)

    # One crawl per website domain, shared by every branch of the same business (chains list
    # 5–10 Maps entries on one site); plus a per-host cap so one slow server can't hog the
    # global semaphore.
    from webscraper.extractors import domain_of
    domain_tasks: dict[str, asyncio.Task] = {}
    host_sems: dict[str, asyncio.Semaphore] = {}

    async def crawl_domain(client: httpx.AsyncClient, website: str,
                           region: str | None = None) -> tuple[Contacts, str | None]:
        dom = domain_of(website) or website
        hs = host_sems.setdefault(dom, asyncio.Semaphore(2))
        async with hs:
            return await crawl_site(client, website, browser_retry, camoufox_retry, region=region)

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=timeout,
                                 limits=limits, verify=False) as client:
        async def one(r: dict[str, Any]) -> None:
            if should_stop():
                return
            key = (r["job_id"], r["place_key"])
            website = r.get("website")
            fields: dict[str, Any] = {"enriched_at": now_iso()}
            if r.get("whatsapp_source") != "maps_link":
                short = await resolve_short_wa(client, r)
                if short:
                    r["whatsapp_number"], r["whatsapp_source"] = short, "maps_link"
            if not website:
                wa_num, wa_src = wa_fallback(r)
                # enrich_error stays NULL: no site means no crawl, which is not a failure.
                fields.update(enrich_status="no_website", enrich_error=None,
                              whatsapp_number=plus(wa_num), whatsapp_source=wa_src)
                store.update_enrichment(*key, fields)
                counts["no_website"] += 1
                if on_progress:
                    on_progress(r, "no_website", fields)
                return
            async with sem:
                try:
                    dom = domain_of(website) or website
                    task = domain_tasks.get(dom)
                    if task is None:
                        # One crawl per domain: the first branch's region parses the
                        # site's bare numbers (branches of one site share a country).
                        task = asyncio.ensure_future(crawl_domain(client, website, region_for(r)))
                        domain_tasks[dom] = task
                    c, reason = await asyncio.shield(task)
                except Exception as e:  # noqa: BLE001 — one bad site must not kill the batch
                    log.warning("enrich failed %s: %s", website, e)
                    wa_num, wa_src = wa_fallback(r)
                    # An exception escaping crawl_site is none of the classified reasons, but
                    # transport_error still picks timeout/dns out of it and calls the rest
                    # 'network' — which beats the nothing this used to record.
                    fields.update(enrich_status="failed", enrich_error=transport_error(e),
                                  whatsapp_number=plus(wa_num), whatsapp_source=wa_src)
                    store.update_enrichment(*key, fields)
                    counts["failed"] += 1
                    if on_progress:
                        on_progress(r, "failed", fields)
                    return
            if c.pages_fetched == 0:
                status = "failed"
            elif c.thin and not (c.emails or c.instagram or c.facebook):
                status = "thin"
            else:
                status = "done"
            if r.get("whatsapp_source") == "maps_link" and r.get("whatsapp_number"):
                wa_num, wa_src = normalise_wa(r["whatsapp_number"], region_for(r)), "maps_link"
            elif c.whatsapp_number:
                wa_num, wa_src = normalise_wa(c.whatsapp_number, region_for(r)), "wa_link"
            else:
                wa_num, wa_src = wa_fallback(r)
            fields.update(
                enrich_status=status,
                # NULL again when the crawl worked, so a successful re-enrich clears a stale
                # reason instead of leaving the lead looking blocked forever.
                enrich_error=crawl_error(c.pages_fetched, reason),
                # Which fetch tier read the home page (httpx | tls | browser | camoufox); None on failure.
                enrich_via=c.via,
                email=c.emails[0] if c.emails else None,
                emails=c.emails,
                instagram=c.instagram, facebook=c.facebook, linkedin=c.linkedin,
                twitter_x=c.twitter_x, youtube=c.youtube, tiktok=c.tiktok,
                # W26: the site's own numbers (E.164 '+'), all offered to WhatsApp verification.
                site_phones=list(c.phones),
                # Stored E.164 WITH the '+' (store.plus) — wa.me links strip it at render.
                whatsapp_number=plus(wa_num), whatsapp_source=wa_src,
            )
            store.update_enrichment(*key, fields)
            counts[status] += 1
            if on_progress:
                on_progress(r, status, fields)

        try:
            await asyncio.gather(*(one(r) for r in rows))
        finally:
            # Chromium outlives the event loop unless we say otherwise, and the enrichment
            # lane calls this once per batch — leaking here would be one browser per batch.
            if browser["fetcher"] is not None:
                await asyncio.to_thread(browser["fetcher"].close)
            if camoufox["fetcher"] is not None:
                await asyncio.to_thread(camoufox["fetcher"].close)
    return counts
