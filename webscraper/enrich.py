"""Website enrichment: fetch a business site (home + contact/about pages), pull emails,
social profiles and a WhatsApp number. Plain HTTP (httpx) — fast, no browser, and it
hits each business's own site, never Google, so it needs no pacing.

When httpx cannot get the page, the reason is recorded (`places.enrich_error`) and, for the
block-shaped ones only, the site is retried once through a real Chromium — see
`browser_fetch.py` for why and how little that path is allowed to cost."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

from webscraper.config import settings
from webscraper.extractors import (
    contact_page_links, extract_emails, extract_socials, extract_whatsapp, is_probably_mobile,
    normalise_wa, region_of_phone,
)
from webscraper.models import Contacts
from webscraper.store import Store, now_iso, plus

log = logging.getLogger("webscraper.enrich")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
           "Accept-Language": "en-IN,en;q=0.9"}
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


def http_error(status_code: int) -> str:
    """'http_403', 'http_404', … — the raw code is kept because the codes mean different
    things to us: 403/429/503 earn a browser retry, 404/410 mean the page is simply gone."""
    return f"http_{status_code}"


def is_block(error: str | None) -> bool:
    """True for the failures a real browser has a chance against."""
    return error in tuple(http_error(c) for c in BLOCK_CODES)


#: getaddrinfo's message differs per platform; all of them mean the same dead domain.
_DNS_MARKERS = ("getaddrinfo", "name or service not known", "nodename nor servname",
                "temporary failure in name resolution", "no address associated",
                "name does not resolve")


def transport_error(e: Exception) -> str:
    """Map an httpx-level failure onto a stored reason.

    'dns' is worth separating out because it is terminal: `atypesourcing.com` (job #6) does
    not resolve, so no retry, browser or otherwise, will ever help — that lead's website is
    dead and only its phone is left. 'timeout' and the 'network' catch-all can both be
    transient, so re-enriching those is worth queueing."""
    if isinstance(e, httpx.TimeoutException):
        return "timeout"
    if any(m in str(e).lower() for m in _DNS_MARKERS):
        return "dns"
    return "network"


def crawl_error(pages_fetched: int, reason: str | None) -> str | None:
    """The reason to store for a finished crawl.

    None once anything at all was fetched — a re-enrich that finally works has to CLEAR the
    stale reason rather than leave last week's 403 sitting on a lead that is fine now.
    'no_pages' covers the odd case where the site answered 200 and still gave us nothing."""
    if pages_fetched > 0:
        return None
    return reason or "no_pages"


async def _fetch_ex(client: httpx.AsyncClient, url: str, retries: int = 1) -> Fetched:
    """GET a page; one retry on transport errors (slow shared hosts time out sporadically).
    Never raises — the failure comes back as `Fetched.error` so the caller can store it."""
    for attempt in range(retries + 1):
        try:
            r = await client.get(url)
            ctype = r.headers.get("content-type", "")
            if r.status_code >= 400:
                return Fetched(error=http_error(r.status_code))
            if ctype and "html" not in ctype and "xml" not in ctype:
                # A PDF/image/JSON "website" — a real response, just nothing to parse.
                return Fetched(error="non_html")
            return Fetched(html=r.text[:MAX_BYTES])
        except httpx.TransportError as e:
            log.debug("fetch failed %s (attempt %d): %s", url, attempt + 1, e)
            if attempt < retries:
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


def _merge(into: Contacts, html: str) -> None:
    for e in extract_emails(html):
        if e not in into.emails:
            into.emails.append(e)
    for net, url in extract_socials(html).items():
        if getattr(into, net) is None:
            setattr(into, net, url)
    if into.whatsapp_number is None:
        into.whatsapp_number = extract_whatsapp(html)


async def crawl_site(client: httpx.AsyncClient, website: str,
                     browser_retry: Callable[[str], Awaitable[str | None]] | None = None,
                     ) -> tuple[Contacts, str | None]:
    """Home page + up to 4 contact/about pages on the same domain.

    Returns the contacts plus the reason nothing was fetched (None when something was).
    `browser_retry`, when given, is the slow path from `browser_fetch.py`: it is offered the
    home page URL once, and only when httpx was blocked rather than merely refused."""
    c = Contacts()
    url = website if "://" in website else "http://" + website
    got = await _fetch_ex(client, url)
    if got.html is None and url.startswith("http://"):
        url_https = "https://" + url[7:]
        alt = await _fetch_ex(client, url_https)
        # Switch to https when it worked, and also when it is the half that got blocked —
        # that is the URL a browser retry should be pointed at (a plain-http fetch of an
        # https-only host fails for boring reasons that say nothing about the real site).
        if alt.html is not None or is_block(alt.error):
            url, got = url_https, alt
        elif got.error is None:
            got = alt
    if got.html is None and is_block(got.error) and browser_retry is not None:
        html = await browser_retry(url)
        if html:
            log.info("browser retry rescued %s (httpx said %s)", url, got.error)
            got = Fetched(html=html)
    if got.html is None:
        return c, got.error or "no_pages"
    home = got.html
    c.pages_fetched = 1
    _merge(c, home)
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
            _merge(c, page)
        if c.emails and (socials_found or c.instagram or c.facebook):
            break
    return c, None


async def enrich_places(store: Store, rows: list[dict[str, Any]], concurrency: int | None = None,
                        country: str | None = None,
                        on_progress: Callable[[dict[str, Any], str], None] | None = None,
                        should_stop: Callable[[], bool] | None = None) -> dict[str, int]:
    """Run crawl_site over `rows` (dicts from Store.places) and write results back.
    `should_stop` is checked before each site; remaining rows stay `pending`."""
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

    async def browser_retry(url: str) -> str | None:
        async with browser_lock:
            if browser["fetcher"] is None:
                if browser["off"]:
                    return None
                try:
                    # Imported here, not at module scope: this keeps Playwright out of the
                    # import graph of every enrich/research run that never needs it.
                    from webscraper.browser_fetch import BROWSER_FALLBACK, BrowserFetcher
                    if not BROWSER_FALLBACK:
                        browser["off"] = True
                        return None
                    browser["fetcher"] = await asyncio.to_thread(BrowserFetcher)
                except Exception as e:                # noqa: BLE001 — no browser, no retry
                    log.warning("browser fallback unavailable (%s) — blocked sites stay failed", e)
                    browser["off"] = True
                    return None
        return await asyncio.to_thread(browser["fetcher"].fetch, url)

    # One crawl per website domain, shared by every branch of the same business (chains list
    # 5–10 Maps entries on one site); plus a per-host cap so one slow server can't hog the
    # global semaphore.
    from webscraper.extractors import domain_of
    domain_tasks: dict[str, asyncio.Task] = {}
    host_sems: dict[str, asyncio.Semaphore] = {}

    async def crawl_domain(client: httpx.AsyncClient, website: str) -> tuple[Contacts, str | None]:
        dom = domain_of(website) or website
        hs = host_sems.setdefault(dom, asyncio.Semaphore(2))
        async with hs:
            return await crawl_site(client, website, browser_retry)

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
                    on_progress(r, "no_website")
                return
            async with sem:
                try:
                    dom = domain_of(website) or website
                    task = domain_tasks.get(dom)
                    if task is None:
                        task = asyncio.ensure_future(crawl_domain(client, website))
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
                        on_progress(r, "failed")
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
                email=c.emails[0] if c.emails else None,
                emails=c.emails,
                instagram=c.instagram, facebook=c.facebook, linkedin=c.linkedin,
                twitter_x=c.twitter_x, youtube=c.youtube, tiktok=c.tiktok,
                # Stored E.164 WITH the '+' (store.plus) — wa.me links strip it at render.
                whatsapp_number=plus(wa_num), whatsapp_source=wa_src,
            )
            store.update_enrichment(*key, fields)
            counts[status] += 1
            if on_progress:
                on_progress(r, status)

        try:
            await asyncio.gather(*(one(r) for r in rows))
        finally:
            # Chromium outlives the event loop unless we say otherwise, and the enrichment
            # lane calls this once per batch — leaking here would be one browser per batch.
            if browser["fetcher"] is not None:
                await asyncio.to_thread(browser["fetcher"].close)
    return counts
