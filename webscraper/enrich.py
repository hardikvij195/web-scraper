"""Website enrichment: fetch a business site (home + contact/about pages), pull emails,
social profiles and a WhatsApp number. Plain HTTP (httpx) — fast, no browser, and it
hits each business's own site, never Google, so it needs no pacing."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

import httpx

from webscraper.config import settings
from webscraper.extractors import (
    contact_page_links, extract_emails, extract_socials, extract_whatsapp, is_probably_mobile,
    normalise_wa, region_of_phone,
)
from webscraper.models import Contacts
from webscraper.store import Store, now_iso

log = logging.getLogger("webscraper.enrich")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
           "Accept-Language": "en-IN,en;q=0.9"}
MAX_BYTES = 1_500_000


async def _fetch(client: httpx.AsyncClient, url: str, retries: int = 1) -> str | None:
    """GET a page; one retry on transport errors (slow shared hosts time out sporadically)."""
    for attempt in range(retries + 1):
        try:
            r = await client.get(url)
            ctype = r.headers.get("content-type", "")
            if r.status_code >= 400 or (ctype and "html" not in ctype and "xml" not in ctype):
                return None
            return r.text[:MAX_BYTES]
        except (httpx.TransportError,) as e:
            log.debug("fetch failed %s (attempt %d): %s", url, attempt + 1, e)
            if attempt < retries:
                await asyncio.sleep(1.5)
                continue
            return None
        except (httpx.HTTPError, UnicodeDecodeError, ValueError) as e:
            log.debug("fetch failed %s: %s", url, e)
            return None
    return None


def _merge(into: Contacts, html: str) -> None:
    for e in extract_emails(html):
        if e not in into.emails:
            into.emails.append(e)
    for net, url in extract_socials(html).items():
        if getattr(into, net) is None:
            setattr(into, net, url)
    if into.whatsapp_number is None:
        into.whatsapp_number = extract_whatsapp(html)


async def crawl_site(client: httpx.AsyncClient, website: str) -> Contacts:
    """Home page + up to 4 contact/about pages on the same domain."""
    c = Contacts()
    url = website if "://" in website else "http://" + website
    home = await _fetch(client, url)
    if home is None and url.startswith("http://"):
        url_https = "https://" + url[7:]
        home = await _fetch(client, url_https)
        if home is not None:
            url = url_https
    if home is None:
        return c
    c.pages_fetched = 1
    _merge(c, home)
    if len(home) < 2_000:
        c.thin = True
    # Contact/about pages are only worth the extra requests when the home page left gaps:
    # no email, or no socials at all. (Slow hosts cost ~5 s per page — see onebodyldn.com.)
    socials_found = sum(1 for k in ("instagram", "facebook", "linkedin", "twitter_x") if getattr(c, k))
    if c.emails and socials_found >= 1:
        return c
    for link in contact_page_links(home, url)[:2]:
        page = await _fetch(client, link)
        if page:
            c.pages_fetched += 1
            _merge(c, page)
        if c.emails and (socials_found or c.instagram or c.facebook):
            break
    return c


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
            return r.get("phone_digits"), "assumed_mobile"
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

    # One crawl per website domain, shared by every branch of the same business (chains list
    # 5–10 Maps entries on one site); plus a per-host cap so one slow server can't hog the
    # global semaphore.
    from webscraper.extractors import domain_of
    domain_tasks: dict[str, asyncio.Task] = {}
    host_sems: dict[str, asyncio.Semaphore] = {}

    async def crawl_domain(client: httpx.AsyncClient, website: str) -> Contacts:
        dom = domain_of(website) or website
        hs = host_sems.setdefault(dom, asyncio.Semaphore(2))
        async with hs:
            return await crawl_site(client, website)

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
                fields.update(enrich_status="no_website", whatsapp_number=wa_num, whatsapp_source=wa_src)
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
                    c = await asyncio.shield(task)
                except Exception as e:  # noqa: BLE001 — one bad site must not kill the batch
                    log.warning("enrich failed %s: %s", website, e)
                    wa_num, wa_src = wa_fallback(r)
                    fields.update(enrich_status="failed", whatsapp_number=wa_num, whatsapp_source=wa_src)
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
                email=c.emails[0] if c.emails else None,
                emails=c.emails,
                instagram=c.instagram, facebook=c.facebook, linkedin=c.linkedin,
                twitter_x=c.twitter_x, youtube=c.youtube, tiktok=c.tiktok,
                whatsapp_number=wa_num, whatsapp_source=wa_src,
            )
            store.update_enrichment(*key, fields)
            counts[status] += 1
            if on_progress:
                on_progress(r, status)

        await asyncio.gather(*(one(r) for r in rows))
    return counts
