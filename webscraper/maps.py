"""Google Maps scraper (Playwright, one persistent Chromium profile, deliberately slow).

Flow: search URL → scroll the results feed collecting place links → visit each place page →
read fields from the side panel. Pacing is configurable; defaults are safe for a home IP.
Selectors use Google's `data-item-id` / aria-label hooks which have been stable for years;
class names are avoided. If Google serves a captcha we back off instead of hammering.
"""
from __future__ import annotations

import hashlib
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote_plus

from playwright.sync_api import Error as PWError, Page, TimeoutError as PWTimeout, sync_playwright

from webscraper.config import settings
from webscraper.extractors import (
    clean_url, country_from_address, domain_of, extract_whatsapp, normalise_phone, normalise_wa,
    region_of_phone,
)
from webscraper.models import Place
from webscraper.browser_recovery import Relauncher, is_closed
from webscraper.store import Store, now_iso

log = logging.getLogger("webscraper.maps")

END_OF_LIST_RE = re.compile(r"reached the end of the list", re.I)
LATLNG_3D4D = re.compile(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)")
LATLNG_AT = re.compile(r"/@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")
PLACE_ID_RE = re.compile(r"!19s(ChIJ[A-Za-z0-9_\-]+)")
CID_RE = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", re.I)
RATING_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*stars?", re.I)
REVIEWS_RE = re.compile(r"([\d,\.]+)\s*reviews?", re.I)
# "4.2 (5,647)" as rendered in the full place panel header
PANEL_RATING_REVIEWS_RE = re.compile(r"(\d\.\d)\s*\(\s*([\d,\.]+)\s*\)")
# "₹200–400" / "$$" / "₹1,000+" right after the review count
PRICE_RANGE_RE = re.compile(r"\)\s*·\s*([₹$€£]\s?[\d,]+(?:\s?[–-]\s?[₹$€£]?\s?[\d,]+)?\+?|[₹$€£]{1,4})(?=\s)")


class CaptchaError(RuntimeError):
    pass


@dataclass
class Pacing:
    delay_sec: float = settings.delay_sec
    pause_every: int = settings.pause_every
    pause_sec: float = settings.pause_sec

    def sleep_between(self) -> None:
        time.sleep(self.delay_sec * random.uniform(0.6, 1.4))

    def maybe_long_pause(self, n: int) -> None:
        if self.pause_every and n and n % self.pause_every == 0:
            t = self.pause_sec * random.uniform(0.7, 1.3)
            log.info("long pause %.0fs after %d places", t, n)
            time.sleep(t)


def _blocked(page: Page) -> bool:
    if "/sorry/" in page.url:
        return True
    try:
        body = page.locator("body").inner_text(timeout=1500)
    except PWTimeout:
        return False
    low = body.lower()
    return "unusual traffic" in low or "i'm not a robot" in low


def _accept_consent(page: Page) -> None:
    """EU-style consent interstitial; no-op elsewhere."""
    try:
        if "consent.google" in page.url:
            for sel in ('button:has-text("Accept all")', 'button:has-text("I agree")',
                        'form[action*="consent"] button', 'button[aria-label*="Accept"]'):
                btn = page.locator(sel).first
                if btn.count():
                    btn.click(timeout=3000)
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                    return
    except PWTimeout:
        pass


def _attr(page: Page, selector: str, attr: str) -> str | None:
    try:
        loc = page.locator(selector).first
        if loc.count():
            return loc.get_attribute(attr, timeout=1500)
    except PWTimeout:
        pass
    return None


def _text(page: Page, selector: str) -> str | None:
    try:
        loc = page.locator(selector).first
        if loc.count():
            t = loc.inner_text(timeout=1500).strip()
            return t or None
    except PWTimeout:
        pass
    return None


def _aria_value(page: Page, selector: str, prefix: str) -> str | None:
    v = _attr(page, selector, "aria-label")
    if v and v.lower().startswith(prefix.lower()):
        v = v[len(prefix):]
    return v.strip(" :") if v else None


def search_url(query: str, location: str | None, lang: str = "en",
               center: tuple[float, float] | None = None, zoom: float | None = None) -> str:
    """Maps search URL. With `center`+`zoom` the results are biased to that viewport, which is
    how a radius is expressed to Maps (there is no radius parameter)."""
    q = f"{query} in {location}" if (location and not center) else query
    if center and zoom:
        return f"https://www.google.com/maps/search/{quote_plus(q)}/@{center[0]:.6f},{center[1]:.6f},{zoom:.1f}z?hl={lang}"
    return f"https://www.google.com/maps/search/{quote_plus(q)}/?hl={lang}"


def zoom_for_radius_km(radius_km: float) -> float:
    """Zoom whose viewport (~1366 px wide) spans roughly 2×radius. z=15 ≈ 1 km, z=12 ≈ 10 km."""
    import math
    radius_km = max(0.3, min(radius_km, 300))
    return max(8.0, min(17.0, 15.0 - math.log2(radius_km)))


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    import math
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def resolve_center(page: Page, query: str, location: str) -> tuple[float, float] | None:
    """Where is `location`? Headless Maps never exposes the map centre (no @lat,lng in the URL,
    og:image is a stale default), so run the plain "query in location" search and take the
    median of the first results' coordinates — they carry !3d<lat>!4d<lng> in their links."""
    try:
        page.goto(search_url(query, location), wait_until="domcontentloaded", timeout=60000)
        _accept_consent(page)
        time.sleep(random.uniform(2, 3))
        cards = collect_place_links(page, 30)
    except (PWTimeout, CaptchaError):
        return None
    pts = [(c.lat, c.lng) for c in cards if c.lat is not None and c.lng is not None]
    if len(pts) < 3:
        return None
    lats = sorted(p[0] for p in pts)
    lngs = sorted(p[1] for p in pts)
    return lats[len(lats) // 2], lngs[len(lngs) // 2]


@dataclass
class FeedCard:
    href: str
    name: str | None = None
    rating: float | None = None
    reviews_count: int | None = None
    lat: float | None = None      # from the href's !3d!4d — lets us skip far places before visiting
    lng: float | None = None

    @property
    def key(self) -> str:
        m = PLACE_ID_RE.search(self.href) or CID_RE.search(self.href)
        return m.group(1) if m else self.href


def grid_centers(center: tuple[float, float], radius_km: float, tile_km: float) -> list[tuple[float, float]]:
    """Square grid of sub-search centres covering a circle. Maps returns ≤~120 results per
    search, so a big radius has to be scraped tile by tile."""
    import math
    lat0, lng0 = center
    step = tile_km * 1.6                           # viewports overlap a little
    out: list[tuple[float, float]] = []
    n = int(math.ceil(radius_km / step))
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            dx, dy = i * step, j * step
            if math.hypot(dx, dy) <= radius_km + tile_km / 2:
                out.append((lat0 + dy / 111.0, lng0 + dx / (111.0 * max(0.2, math.cos(math.radians(lat0))))))
    out.sort(key=lambda c: haversine_km(lat0, lng0, c[0], c[1]))   # centre first, spiral out
    return out[:150]


# One round-trip per sweep: for every place link in the feed, walk up to the card and read the
# "4.8 stars 1,263 Reviews" aria-label. The place panel itself no longer shows a review count in
# the layout Google serves to headless sessions, so the feed is where we get it.
_FEED_JS = """
() => {
  const feed = document.querySelector('div[role="feed"]');
  if (!feed) return [];
  const out = [];
  for (const a of feed.querySelectorAll('a[href*="/maps/place/"]')) {
    let card = a.parentElement, img = null;
    for (let i = 0; i < 8 && card; i++) {
      img = card.querySelector('span[role="img"][aria-label*="star" i], span[role="img"][aria-label*="review" i]');
      if (img) break;
      card = card.parentElement;
    }
    out.push({ href: a.href, name: a.getAttribute('aria-label'), label: img ? img.getAttribute('aria-label') : null });
  }
  return out;
}
"""


def _parse_feed_label(label: str | None) -> tuple[float | None, int | None]:
    if not label:
        return None, None
    rating = reviews = None
    m = RATING_RE.search(label)
    if m:
        try:
            rating = float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    m = REVIEWS_RE.search(label)
    if m:
        try:
            reviews = int(re.sub(r"[^\d]", "", m.group(1)))
        except ValueError:
            pass
    return rating, reviews


def collect_place_links(page: Page, max_places: int,
                        on_progress: Callable[[int], None] | None = None) -> list[FeedCard]:
    """Scroll the results feed until `max_places` links or end of list."""
    links: dict[str, FeedCard] = {}
    try:
        page.wait_for_selector('div[role="feed"]', timeout=15000)
    except PWTimeout:
        if "/maps/place/" in page.url:           # Maps jumped straight to a single result
            return [FeedCard(href=page.url)]
        if _blocked(page):
            raise CaptchaError("blocked on search page")
        return []
    feed = page.locator('div[role="feed"]')

    def sweep() -> None:
        try:
            cards = page.evaluate(_FEED_JS)
        except PWTimeout:
            return
        for c in cards:
            href = c.get("href")
            if not href or href in links:
                continue
            rating, reviews = _parse_feed_label(c.get("label"))
            lat = lng = None
            m = LATLNG_3D4D.search(href)
            if m:
                lat, lng = float(m.group(1)), float(m.group(2))
            links[href] = FeedCard(href=href, name=c.get("name"), rating=rating, reviews_count=reviews, lat=lat, lng=lng)
            if len(links) >= max_places:
                break

    stale_rounds = 0
    while len(links) < max_places:
        sweep()
        if on_progress:
            on_progress(len(links))
        if len(links) >= max_places:
            break
        before = len(links)
        feed.evaluate("el => el.scrollBy(0, el.scrollHeight)")
        time.sleep(random.uniform(1.5, 3.5))
        try:
            if END_OF_LIST_RE.search(feed.inner_text(timeout=1500) or ""):
                sweep()                              # catch the last batch rendered after the marker
                break
        except PWTimeout:
            pass
        stale_rounds = stale_rounds + 1 if len(links) == before else 0
        if stale_rounds >= 6:
            log.info("feed stopped growing at %d", len(links))
            break
        if _blocked(page):
            raise CaptchaError("blocked while scrolling")
    return list(links.values())[:max_places]


_WA_HREF_RE = re.compile(r"wa\.me|wa\.link|whatsapp\.com|whatsapp://", re.I)


def _panel_whatsapp_links(page: Page) -> list[str]:
    """Any WhatsApp-ish links Google shows on the place panel (chat button, booking link)."""
    try:
        hrefs = page.locator('div[role="main"]').first.evaluate(
            "el => [...el.querySelectorAll('a[href]')].map(a => a.href)", timeout=3000)
    except PWTimeout:
        return []
    return [h for h in hrefs if h and _WA_HREF_RE.search(h)]


def scrape_place(page: Page, href: str, job_id: int, country: str) -> Place:
    page.goto(href, wait_until="domcontentloaded", timeout=45000)
    _accept_consent(page)
    if _blocked(page):
        raise CaptchaError("blocked on place page")
    try:
        page.wait_for_selector('div[role="main"] h1', timeout=15000)
    except PWTimeout:
        pass

    name = _text(page, 'div[role="main"] h1')
    category = _text(page, 'button[jsaction*="category"]')
    address = _aria_value(page, 'button[data-item-id="address"]', "Address:")
    website = clean_url(_attr(page, 'a[data-item-id="authority"]', "href"))
    plus_code = _aria_value(page, 'button[data-item-id="oloc"]', "Plus code:")
    pid = _attr(page, 'button[data-item-id^="phone:tel:"]', "data-item-id")
    phone_raw = pid.split("phone:tel:", 1)[1] if pid else _aria_value(page, 'button[data-item-id^="phone"]', "Phone:")

    # Rating comes from the stars aria-label. Review count + price range only render in the
    # full (headed) layout — Google serves a lite panel to headless sessions — so they stay
    # None under --headless and fill in under --no-headless.
    rating = reviews = None
    price_range = None
    main_html = main_text = ""
    try:
        main = page.locator('div[role="main"]').first
        main_html = main.inner_html(timeout=3000)
        main_text = main.inner_text(timeout=3000)
    except PWTimeout:
        pass
    m = RATING_RE.search(main_html)
    if m:
        try:
            rating = float(m.group(1).replace(",", "."))
        except ValueError:
            rating = None
    m = PANEL_RATING_REVIEWS_RE.search(main_text) if main_text else None
    if m:
        try:
            reviews = int(re.sub(r"[^\d]", "", m.group(2)))
            if rating is None:
                rating = float(m.group(1))
        except ValueError:
            pass
    m = PRICE_RANGE_RE.search(main_text) if main_text else None
    if m:
        price_range = m.group(1).strip()

    url = page.url
    lat = lng = None
    m = LATLNG_3D4D.search(url) or LATLNG_AT.search(url)
    if m:
        lat, lng = float(m.group(1)), float(m.group(2))
    mp = PLACE_ID_RE.search(url) or PLACE_ID_RE.search(href)
    place_id = mp.group(1) if mp else None
    mc = CID_RE.search(url) or CID_RE.search(href)
    cid = mc.group(1) if mc else None
    key = place_id or cid or hashlib.sha1(href.encode()).hexdigest()[:16]

    # Country: the address names it for foreign places ("…, United Kingdom"); Maps drops it
    # for places in the browser's own region, so fall back to the job's country.
    place_country = country_from_address(address) or country
    phone_e164, phone_digits = normalise_phone(phone_raw, place_country)
    wa_links = _panel_whatsapp_links(page)
    wa_number = None
    wa_region = region_of_phone(phone_e164, place_country)
    for link in wa_links:
        wa_number = normalise_wa(extract_whatsapp(link), wa_region)
        if wa_number:
            break
    return Place(
        job_id=job_id, place_key=key, name=name, category=category, address=address,
        country=place_country, phone=phone_e164 or phone_raw, phone_digits=phone_digits,
        website=website, domain=domain_of(website), rating=rating, reviews_count=reviews,
        price_range=price_range,
        lat=lat, lng=lng, maps_url=url, plus_code=plus_code, place_id=place_id,
        whatsapp_number=wa_number, whatsapp_source="maps_link" if wa_number else None,
        scraped_at=now_iso(),
        raw={"cid": cid, "href": href, "phone_raw": phone_raw, "maps_wa_links": wa_links},
    )


def run_scrape(store: Store, job_id: int, query: str, location: str | None, max_places: int,
               pacing: Pacing, headless: bool | None = None, country: str | None = None,
               on_event: Callable[[str, dict], None] | None = None,
               should_stop: Callable[[], bool] | None = None,
               radius_km: float | None = None,
               wait_if_paused: Callable[[], None] | None = None,
               center: tuple[float, float] | None = None,
               known_keys: set[str] | None = None,
               collect_until: float | None = None,
               collect_target: int | None = None) -> int:
    """Scrape up to `max_places` (0 = unlimited) for (query, location) into `store`. Returns count saved.
    `should_stop` is polled between places; when it returns True the job is marked stopped.
    `radius_km` (needs `location`) centres the search there, tiles the circle when more than one
    Maps page of results is wanted, and skips farther places.
    `wait_if_paused` is called between places and may block (run-time window).
    `collect_until` (time.monotonic deadline) caps the link-collection phase: a wide radius
    tiles into thousands of sub-searches, and without a cap the job spends its whole time
    budget collecting links and scrapes nothing at all.
    `collect_target` stops collection once there are more links than the remaining time could
    ever visit — past that point every extra tile is time stolen from actual scraping."""
    headless = settings.headless if headless is None else headless
    country = country or settings.default_country
    emit = on_event or (lambda kind, data: None)
    should_stop = should_stop or (lambda: False)
    wait_if_paused = wait_if_paused or (lambda: None)
    unlimited = max_places <= 0
    limit = 10**9 if unlimited else max_places
    queries = [q.strip() for q in (query or "").split(",") if q.strip()] or [(query or "").strip()]
    settings.profile_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    with sync_playwright() as pw:
        launch_kwargs: dict = dict(
            user_data_dir=str(settings.profile_dir), headless=headless, locale="en-IN",
            viewport={"width": 1366, "height": 850},
            args=["--disable-blink-features=AutomationControlled", "--lang=en-IN"],
        )
        if settings.maps_proxy:
            launch_kwargs["proxy"] = {"server": settings.maps_proxy}

        def _open():
            c = pw.chromium.launch_persistent_context(**launch_kwargs)
            # images/fonts/media add nothing we read — skipping them cuts bandwidth ~80%
            c.route(re.compile(r"\.(png|jpe?g|gif|webp|svg|woff2?|ttf|mp4|webm)(\?|$)", re.I),
                    lambda route: route.abort())
            pg = c.pages[0] if c.pages else c.new_page()
            pg.set_default_timeout(20000)
            return c, pg

        # A headed run shares a desktop with a human, and Chrome also dies on its own
        # (OOM, a crashed renderer, a Windows update). Losing the browser used to abort
        # the whole job and throw away every place already collected - job #3 died exactly
        # that way, mid-feed, with TargetClosedError. Relaunching costs one profile reload;
        # the alternative is losing the run. Capped so a browser that cannot start at all
        # still fails fast instead of looping.
        # The mechanism moved to browser_recovery.py (2026-08-23) so the WhatsApp lane,
        # which had none, gets the same treatment. Behaviour here is unchanged.
        _relauncher = Relauncher(
            _open, on_restart=lambda where, n: emit("browser_restart", {"where": where, "attempt": n}))
        ctx, page = _relauncher.open()
        _is_closed = is_closed

        def _recover(where: str) -> bool:
            nonlocal ctx, page
            if not _relauncher.recover(where):
                return False
            ctx, page = _relauncher.current
            return True

        try:
            zoom: float | None = None
            if radius_km and (center or location):
                if not center:                     # no pinned centre from the map picker → ask Maps
                    center = resolve_center(page, queries[0], location or "")
                if center:
                    zoom = zoom_for_radius_km(radius_km)
                    emit("center", {"lat": center[0], "lng": center[1], "zoom": zoom})
                else:
                    emit("center_failed", {})
                time.sleep(random.uniform(1, 2))
            # One Maps search returns ~120 places max. For "unlimited" or big asks inside a
            # radius, tile the circle with ~2 km sub-searches and merge.
            MAPS_PAGE_CAP = 110
            tiling = bool(center and radius_km) and (unlimited or max_places > MAPS_PAGE_CAP)
            if tiling:
                tile_km = min(float(radius_km), 2.0)
                centers = grid_centers(center, float(radius_km), tile_km)
                tile_zoom = zoom_for_radius_km(tile_km)
                emit("tiles", {"count": len(centers)})
            else:
                centers = [center]
                tile_zoom = zoom

            merged: dict[str, FeedCard] = {}
            skipped_far = skipped_known = 0
            # A job can carry several comma-separated keywords ("dentist, orthodontist"); each is
            # its own Maps search, all merged into one deduped lead set.
            # Centre-major, NOT query-major: a 50 km radius tiles into ~150 cells and a
            # 52-keyword job is 7,800 searches, far more than any run finishes. Ordered by
            # query it would collect every tile of keyword 1 and never reach keyword 2, so a
            # truncated run returned one category. This way each centre sweeps all keywords,
            # and cutting the phase short still leaves every keyword represented.
            steps = [(qy, c) for c in centers for qy in queries]
            budget_hit = False
            for s_i, (qy, c) in enumerate(steps, 1):
                if should_stop() or len(merged) >= limit:
                    break
                # Stop collecting while there is still time left to actually scrape the
                # places found. Without this the tile loop eats the entire time limit and
                # the job ends with links but zero leads.
                if collect_until is not None and time.monotonic() >= collect_until:
                    budget_hit = True
                    emit("links_budget", {"count": len(merged), "tile": s_i,
                                          "tiles": len(steps), "reason": "time"})
                    break
                if collect_target is not None and len(merged) >= collect_target:
                    budget_hit = True
                    emit("links_budget", {"count": len(merged), "tile": s_i,
                                          "tiles": len(steps), "reason": "enough"})
                    break
                wait_if_paused()
                want = 10**6 if (tiling or unlimited or (center and radius_km)) else max_places
                # Retry this tile through the relauncher until it reads or the relaunch
                # cap is spent. Everything already in `merged` survives, so a mid-run
                # crash costs one tile, not the job. This used to be a single unguarded
                # retry: job #13 (2026-08-24) relaunched once, the human closed the new
                # window too, and the second TargetClosedError killed the whole lane.
                while True:
                    try:
                        page.goto(search_url(qy, location, center=c, zoom=tile_zoom),
                                  wait_until="domcontentloaded", timeout=60000)
                        _accept_consent(page)
                        time.sleep(random.uniform(2, 4))
                        cards = collect_place_links(page, want,
                                                    on_progress=lambda n: emit("links", {"count": len(merged) + n, "tile": s_i, "tiles": len(steps)}))
                        break
                    except PWError as e:
                        if not _is_closed(e) or not _recover(f"tile {s_i}/{len(steps)}"):
                            raise
                for card in cards:
                    if card.key in merged:
                        continue
                    if known_keys and card.key in known_keys:   # 'only new businesses' job
                        skipped_known += 1
                        continue
                    # pre-filter on the coordinates embedded in the link — no visit needed
                    if center and radius_km and card.lat is not None:
                        d = haversine_km(center[0], center[1], card.lat, card.lng)
                        if d > radius_km:
                            skipped_far += 1
                            continue
                    merged[card.key] = card
                emit("links", {"count": len(merged), "tile": s_i, "tiles": len(steps)})
                if len(steps) > 1:
                    time.sleep(random.uniform(1.5, 3.5))
            links = list(merged.values())[:limit]
            emit("links_done", {"count": len(links), "skipped_far": skipped_far,
                                "skipped_known": skipped_known, "budget_hit": budget_hit})
            known = store.known_place_keys(job_id)
            for i, card in enumerate(links, 1):
                if saved >= limit:
                    break
                if should_stop():
                    emit("abort", {"reason": "stopped by user"})
                    store.finish_job(job_id, "stopped", "stopped by user")
                    return saved
                wait_if_paused()
                href = card.href
                pacing.sleep_between()
                try:
                    place = scrape_place(page, href, job_id, country)
                except CaptchaError:
                    backoff = random.uniform(900, 1800)
                    emit("captcha", {"backoff_sec": backoff})
                    log.warning("captcha — backing off %.0fs", backoff)
                    time.sleep(backoff)
                    try:
                        place = scrape_place(page, href, job_id, country)
                    except CaptchaError:
                        emit("abort", {"reason": "captcha twice"})
                        store.finish_job(job_id, "stopped", "captcha twice")
                        return saved
                except PWTimeout:
                    emit("skip", {"href": href, "reason": "timeout"})
                    continue
                except PWError as e:
                    # Same recovery as the tile loop: a dead browser costs this one place,
                    # not the remaining list and not the places already saved.
                    if not _is_closed(e) or not _recover(f"place {i}/{len(links)}"):
                        raise
                    emit("skip", {"href": href, "reason": "browser restarted"})
                    continue
                # feed card values fill whatever the panel didn't expose
                if place.name is None and card.name:
                    place.name = card.name
                if place.rating is None and card.rating is not None:
                    place.rating = card.rating
                if place.reviews_count is None and card.reviews_count is not None:
                    place.reviews_count = card.reviews_count
                if center and place.lat is not None and place.lng is not None:
                    place.distance_km = round(haversine_km(center[0], center[1], place.lat, place.lng), 2)
                    if radius_km and place.distance_km > radius_km * 1.05:   # feed coords were approximate
                        skipped_far += 1
                        emit("far", {"name": place.name, "distance_km": place.distance_km, "skipped": skipped_far})
                        continue
                if place.place_key in known:
                    emit("dup", {"name": place.name})
                store.upsert_place(place)
                known.add(place.place_key)
                saved += 1
                emit("place", {"i": i, "n": len(links), "saved": saved, "place": place})
                pacing.maybe_long_pause(i)
        finally:
            ctx.close()
    store.finish_job(job_id, "done")
    return saved
