"""Fetch ONE page through a real Chromium, for the sites plain httpx cannot get past.

Why this exists: live CRM job #6 (Greater London) finished with 9 leads at
`enrich_status='failed'`. Re-running all 9 by hand showed 8 were WAF/Cloudflare bot
blocks - `lookers.co.uk`, `hrowen.co.uk` (3 leads), `carluv.co.uk`,
`mayfairmotorsolutions.com`, `maryleboneminicabs.co.uk`, `luxurycarsltd.co.uk` all answer
httpx with a flat 403 while a real browser loads them fine. (The 9th, `atypesourcing.com`,
simply does not resolve any more - no browser fixes a dead domain.)

This is a SLOW PATH and must stay one: httpx costs ~0.3 s/site, a browser round trip ~5 s,
and enrichment only keeps up with discovery because of that ratio. `enrich.py` calls in here
for a single site only after httpx came back block-shaped (403/429/503), once, and never
constructs a fetcher until the first such site appears - a job with no blocked sites never
launches Chromium at all.

Threading: the Playwright *sync* API refuses to run inside a thread that owns an asyncio
loop, and `enrich_places` is async. So the browser lives on one dedicated worker thread and
`fetch()` hands it a URL over a queue and blocks. That also serialises the work, which is
what we want anyway - one context, one page, one blocked site at a time. The async caller
wraps `fetch` in `asyncio.to_thread`.
"""
from __future__ import annotations

import logging
import os
import queue
import re
import threading
from typing import Any, Callable

from webscraper.browser_recovery import Relauncher, is_closed
from webscraper.config import _bool, settings

log = logging.getLogger("webscraper.browser_fetch")

# Prefer patchright — a drop-in Playwright fork that strips the automation tells
# (`navigator.webdriver`, the CDP `Runtime.enable` leak, a handful of headless
# giveaways) that Cloudflare fingerprints even a real Chrome by. API-identical, so the
# rest of this file is unchanged. Falls back to stock Playwright when patchright is not
# installed, so nothing breaks without it.
try:
    from patchright.sync_api import Error as PWError, sync_playwright  # type: ignore
    STEALTH = True
except Exception:  # noqa: BLE001 — patchright optional
    from playwright.sync_api import Error as PWError, sync_playwright
    STEALTH = False

#: Default ON - the whole point is that a blocked site stops being a dead lead.
#: `ENRICH_BROWSER_FALLBACK=false` in `.env` turns the slow path off entirely.
BROWSER_FALLBACK = _bool(os.getenv("ENRICH_BROWSER_FALLBACK"), True)
#: Headless is fine for ordinary sites (unlike WhatsApp Web, which re-QRs a headless
#: profile, and unlike Maps, which serves headless a lite panel) - a business site renders
#: the same either way.
#:
#: It is NOT fine against a Cloudflare interactive challenge. Measured on job #7: the
#: headless retry rescued some 403s (hrowen.co.uk, carluv.co.uk) but 15 remained, and
#: those are the ones that want a real browser fingerprint. `ENRICH_BROWSER_HEADLESS=false`
#: in `.env` trades a visible window on the agent PC for a materially better pass rate.
#:
#: Scope note (updated 2026-08-24, by user directive): the earlier "will not spoof
#: fingerprints" stance is lifted for THIS use — reading a PUBLIC business page for
#: contact info. The honest, cheap route the user asked for is "use my own machine": the
#: user's REAL Chrome (`channel="chrome"`, not the bundled chromium), headed, from a home
#: residential IP, with patchright hiding the automation flags. That looks like an
#: ordinary person browsing and clears most Cloudflare without proxies or CAPTCHA solving.
#: Still NOT done: solving CAPTCHAs, or attacking anything non-public.
HEADLESS = _bool(os.getenv("ENRICH_BROWSER_HEADLESS"), True)

#: Use the machine's real Google Chrome instead of Playwright's bundled Chromium. Real
#: Chrome carries a real fingerprint (TLS, UA, feature flags) that Cloudflare trusts far
#: more than headless-shell. Default ON; falls back to bundled Chromium automatically when
#: Chrome is not installed. `ENRICH_BROWSER_REAL_CHROME=false` forces the bundle.
USE_REAL_CHROME = _bool(os.getenv("ENRICH_BROWSER_REAL_CHROME"), True)

#: Its OWN profile dir: Chromium locks a user-data-dir, and a Maps scrape is usually still
#: running in the other lane on `settings.profile_dir`.
PROFILE_DIR = settings.profile_dir.parent / "fetch-profile"

#: Same trick maps.py uses - images/fonts/media add nothing we read and cut bandwidth ~80%.
_ASSETS = re.compile(r"\.(png|jpe?g|gif|webp|svg|woff2?|ttf|mp4|webm)(\?|$)", re.I)

NAV_TIMEOUT_MS = 25_000
#: Cloudflare's interstitial solves itself in JS and then swaps the document; without a
#: settle the page we read is still the "Just a moment" challenge.
SETTLE_MS = 1_500
#: If the settle left a challenge page up, re-read for up to this long in these steps —
#: a JS challenge that auto-solves usually does so within ~6 s; longer is a managed
#: Turnstile / hard deny no browser passes, so we stop rather than wait forever.
CHALLENGE_WAIT_MS = 6_000
CHALLENGE_STEP_MS = 1_500
BOOT_TIMEOUT_SEC = 90.0
#: Generous because calls queue behind each other on the single worker thread.
FETCH_TIMEOUT_SEC = 180.0
MAX_BYTES = 1_500_000

_BLOCK_MARKERS = ("just a moment", "attention required! | cloudflare", "checking your browser",
                  "access denied", "error 1015", "request blocked", "are you a robot")


def _proxy_arg(url: str | None = None) -> dict[str, str] | None:
    """A proxy URL ('http://user:pass@host:port') as Playwright's proxy dict, or None when no
    proxy is set. Defaults to `settings.enrich_proxy` (W13 behaviour); W15 callers pass the
    proxy the pool picked. Playwright wants the credentials split out from the server."""
    from webscraper.proxies import proxy_arg
    raw = settings.enrich_proxy if url is None else url
    if not raw:
        return None
    out = proxy_arg(raw)
    if out is None:
        log.warning("ENRICH_PROXY is set but unparseable — ignoring")
    return out


def looks_blocked(html: str | None) -> bool:
    """True when the browser got a challenge/deny page rather than the business site.
    Cheaper and more honest than trusting the HTTP status: Cloudflare answers 403 first and
    then 200s the real page after its JS runs, so the status alone would reject good fetches."""
    if not html:
        return True
    head = html[:4000].lower()
    return any(m in head for m in _BLOCK_MARKERS)


class BrowserFetcher:
    """One long-lived Chromium context, reused across calls, driven from one worker thread.

    Construction launches the browser (~1-2 s) - do it lazily, and off the event loop.
    Use as a context manager, or call `close()`; the thread is a daemon so a forgotten
    fetcher cannot keep the process alive, but the profile lock would linger.
    """

    def __init__(self, headless: bool | None = None, proxy: str | None = None) -> None:
        self._headless = HEADLESS if headless is None else headless
        #: Proxy URL for this browser (W15 pool pick); None = ENRICH_PROXY / direct. A
        #: persistent context binds its proxy at launch, so rotation happens per launch.
        self._proxy = proxy
        self._jobs: queue.Queue[tuple[str, list[str | None], threading.Event] | None] = queue.Queue()
        self._ready = threading.Event()
        self._boot_error: BaseException | None = None
        self._start_lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="browser-fetch", daemon=True)
        self._thread.start()
        if not self._ready.wait(BOOT_TIMEOUT_SEC):
            raise RuntimeError(f"browser fetch did not start within {BOOT_TIMEOUT_SEC:.0f}s")
        if self._boot_error is not None:
            raise RuntimeError(f"browser fetch could not start: {self._boot_error}")

    # -- public --------------------------------------------------------------------
    def __enter__(self) -> BrowserFetcher:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive() and not self._closed

    def fetch(self, url: str) -> str | None:
        """HTML of `url`, or None if the browser could not get it either. Blocking - call it
        from a worker thread (`asyncio.to_thread`), never from the event loop."""
        if not self.alive:
            return None
        slot: list[str | None] = []
        done = threading.Event()
        self._jobs.put((url, slot, done))
        if not done.wait(FETCH_TIMEOUT_SEC):
            log.warning("browser fetch timed out waiting for %s", url)
            return None
        return slot[0] if slot else None

    def close(self) -> None:
        with self._start_lock:
            if self._closed:
                return
            self._closed = True
        self._jobs.put(None)
        self._thread.join(timeout=30)

    # -- worker thread -------------------------------------------------------------
    def _run(self) -> None:
        try:
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as pw:
                rl = Relauncher(self._opener(pw))
                rl.open()
                self._ready.set()
                try:
                    while True:
                        item = self._jobs.get()
                        if item is None:
                            return
                        url, slot, done = item
                        try:
                            slot.append(self._goto(rl, url))
                        except Exception:                          # noqa: BLE001
                            log.debug("browser fetch crashed on %s", url, exc_info=True)
                            slot.append(None)
                        finally:
                            done.set()
                finally:
                    rl.close()
        except BaseException as e:                                 # noqa: BLE001
            # Playwright not installed, no browser binary, profile locked... whatever it is,
            # the caller must find out instead of blocking on a thread that never answers.
            self._boot_error = e
            log.warning("browser fetch worker died: %s", e)
        finally:
            self._ready.set()
            self._drain()

    def _opener(self, pw: Any) -> Callable[[], tuple[Any, Any]]:
        proxy = _proxy_arg(self._proxy)

        def _launch(use_chrome: bool) -> Any:
            kw: dict[str, Any] = dict(
                user_data_dir=str(PROFILE_DIR), headless=self._headless, locale="en-GB",
                viewport={"width": 1366, "height": 850},
                # Auto-proceed past cert warnings. Many small business sites have a misissued
                # or wrong-CN certificate (newsquaredentist.com, camskinclinic.com on job 11
                # both threw ERR_CERT_COMMON_NAME_INVALID) — a human would click "proceed",
                # so the crawler does the same rather than stalling on the interstitial. We
                # only ever READ a public page, so a bad cert is a data-quality signal, not a
                # security decision.
                ignore_https_errors=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            # channel="chrome" = the machine's installed Google Chrome, not the bundled
            # chromium; a real fingerprint Cloudflare trusts. Omitted when Chrome is absent.
            if use_chrome:
                kw["channel"] = "chrome"
            if proxy:
                kw["proxy"] = proxy
            return pw.chromium.launch_persistent_context(**kw)

        def _open() -> tuple[Any, Any]:
            try:
                ctx = _launch(USE_REAL_CHROME)
            except Exception as e:  # noqa: BLE001 — Chrome not installed / channel unusable
                if not USE_REAL_CHROME:
                    raise
                log.info("real Chrome unavailable (%s) — falling back to bundled Chromium", e)
                ctx = _launch(False)
            log.debug("browser fetch launched (chrome=%s, stealth=%s, proxy=%s)",
                      USE_REAL_CHROME, STEALTH, proxy["server"] if proxy else None)
            ctx.route(_ASSETS, lambda route: route.abort())
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg.set_default_timeout(NAV_TIMEOUT_MS)
            return ctx, pg
        return _open

    def _goto(self, rl: Relauncher, url: str) -> str | None:
        """Navigate + settle + read. One relaunch retry, on the same terms as the Maps lane:
        a dead Chromium is worth rebuilding, a misbehaving page is not."""
        for attempt in (0, 1):
            _ctx, page = rl.current
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                page.wait_for_timeout(SETTLE_MS)
                html = page.content()
                # A Cloudflare JS challenge swaps the document for the real page once its
                # script solves — but that can take 3-5 s, past the initial settle. Re-read
                # a few times before giving up; a challenge that never clears (a managed
                # Turnstile, or a hard 1020 deny) still bails at CHALLENGE_WAIT_MS instead
                # of hanging. Auto-solvers are rescued; unsolvable walls cost a few seconds.
                waited = 0
                while looks_blocked(html) and waited < CHALLENGE_WAIT_MS:
                    page.wait_for_timeout(CHALLENGE_STEP_MS)
                    waited += CHALLENGE_STEP_MS
                    html = page.content()
                if looks_blocked(html):
                    log.debug("browser fetch still blocked after %dms: %s", SETTLE_MS + waited, url)
                    return None
                return html[:MAX_BYTES]
            except PWError as e:
                if attempt == 0 and is_closed(e) and rl.recover(f"fetch {url}"):
                    continue
                log.debug("browser fetch failed %s: %s", url, e)
                return None
        return None

    def _drain(self) -> None:
        """Release anyone already queued - a dead worker must not hang the enrichment run."""
        while True:
            try:
                item = self._jobs.get_nowait()
            except queue.Empty:
                return
            if item is not None:
                item[2].set()
