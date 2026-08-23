"""Verify whether a phone number is on WhatsApp, using logged-in WhatsApp Web sessions.

Careful by design — this drives a *real* WhatsApp account, so:
  * It NEVER sends a message. It opens web.whatsapp.com/send?phone=<num>, reads whether
    WhatsApp accepts the number (chat opens) or rejects it (invalid popup), then leaves.
  * Each account is one persistent browser profile under data/wa-profiles/<name>/ — the
    session (IndexedDB/localStorage, not just cookies) is saved to disk, so you scan the
    QR once per account and it stays linked as a "linked device".
  * A per-account daily cap (settings.wa_daily_cap) and a randomised delay
    (settings.wa_delay_min..max) between checks keep the traffic human-paced. Multiple
    accounts raise the ceiling: total/day = cap x number of enabled accounts.
  * Login is always headed (you must see the QR). Verification defaults to headless.
  * A Chrome that dies mid-run (OOM, crashed renderer, Windows update, a human closing a
    headed window) is relaunched on the SAME profile dir and the one number in flight is
    retried - see `_check`. Before 2026-08-23 this step ran last in a sequence, so a dead
    browser only cost the tail; it is now a long-lived concurrent lane, where the same
    crash killed the whole lane.
  * Numbers are BARE digits inside the send URL and carry a leading '+' everywhere they
    are persisted or reported (user directive 2026-08-23).

Ban risk is never zero - use a spare number, not your main business WhatsApp.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import date
from typing import Any, Callable

from playwright.sync_api import Error as PWError, Page, TimeoutError as PWTimeout, sync_playwright

from webscraper.browser_recovery import MAX_RELAUNCH, Relauncher, is_closed
from webscraper.config import settings
from webscraper.extractors import normalise_phone
from webscraper.store import Store, plus

log = logging.getLogger("webscraper.wa_verify")

WA_SEND = "https://web.whatsapp.com/send?phone={num}&text&type=phone_number&app_absent=0"
_LOGIN_TIMEOUT_MS = 120_000     # QR scan grace
_CHECK_TIMEOUT_MS = 25_000      # per-number decision grace
# WhatsApp's "this number isn't registered" popup — exact wording varies, so match any.
_NOT_ON_WA = ("isn't on whatsapp", "not on whatsapp", "is invalid", "shared via url",
              "phone number shared", "no está en whatsapp")


class WaNotLoggedIn(RuntimeError):
    """The account's profile has no live WhatsApp Web session (needs `wa-login`)."""


def profile_dir(name: str):
    d = settings.wa_profiles_dir / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _e164_digits(phone: str | None, wa_number: str | None, country: str | None) -> str | None:
    """Best full international number for a place - BARE digits, deliberately no '+'.

    web.whatsapp.com/send?phone= wants them bare, so this stays the URL-shaped form.
    Anything that leaves this module for the DB or a progress callback goes through
    `store.plus()` first (user directive 2026-08-23: every WA number shows a '+').
    """
    if wa_number:
        d = "".join(ch for ch in wa_number if ch.isdigit())
        if 8 <= len(d) <= 15:
            return d
    e164, digits = normalise_phone(phone, (country or settings.default_country or "IN").upper())
    if e164:
        return e164.lstrip("+")
    return digits if digits and 8 <= len(digits) <= 15 else None


# -- logged-in-state + per-number decision on WhatsApp Web ----------------------
def _is_logged_in(page: Page) -> bool:
    """Chat list present (logged in) vs the QR / link-device landing (not)."""
    try:
        page.wait_for_selector(
            'div[aria-label="Chat list"], [data-testid="chat-list"], '
            'canvas[aria-label*="Scan"], [data-testid="qrcode"], div[data-ref]',
            timeout=40_000)
    except PWTimeout:
        return False
    for sel in ('div[aria-label="Chat list"]', '[data-testid="chat-list"]',
                'header [data-testid="menu-bar-menu"]', '#pane-side'):
        if page.locator(sel).count():
            return True
    return False


def _decide(page: Page) -> str:
    """On an already-loaded send URL, return 'yes' | 'no' | 'unknown'.

    Valid number      -> WhatsApp opens the conversation (#main chat pane appears).
    Not-on-WhatsApp    -> a popup, e.g. "The number +44 … isn't on WhatsApp." (~2 s).
    """
    deadline = time.monotonic() + _CHECK_TIMEOUT_MS / 1000
    compose_sel = ('footer div[contenteditable="true"][data-tab], '
                   'div[contenteditable="true"][data-tab], div[title="Type a message"]')
    while time.monotonic() < deadline:
        dlg = page.locator('div[role="dialog"]')
        if dlg.count():
            try:
                txt = dlg.first.inner_text().lower()
            except Exception:  # noqa: BLE001
                txt = ""
            if any(p in txt for p in _NOT_ON_WA):
                return "no"
        # Chat pane / compose box present => the number opened a conversation => on WA.
        if page.locator('#main').count() or page.locator(compose_sel).count():
            return "yes"
        # Session dropped mid-run (rare) — surface as not-logged-in so the caller stops.
        if page.locator('canvas[aria-label*="Scan"], [data-testid="qrcode"]').count():
            raise WaNotLoggedIn("session ended mid-run")
        page.wait_for_timeout(400)
    return "unknown"


def _dismiss_popup(page: Page) -> None:
    """Close the invalid-number popup so the next number starts clean."""
    for sel in ('div[role="dialog"] button:has-text("OK")',
                '[data-testid="popup-controls-ok"]',
                'div[role="button"]:has-text("OK")'):
        loc = page.locator(sel)
        if loc.count():
            try:
                loc.first.click(timeout=2000)
            except PWTimeout:
                pass
            return


# -- login (headed, one-time per account) ---------------------------------------
def login(name: str) -> bool:
    """Open WhatsApp Web headed; wait for the QR to be scanned. Returns True on success."""
    Store().add_wa_account(name)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir(name)), headless=False, locale="en",
            viewport={"width": 1100, "height": 820},
            args=["--disable-blink-features=AutomationControlled"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://web.whatsapp.com/", timeout=60_000)
        log.info("[%s] scan the QR in the window (2 min)...", name)
        try:
            page.wait_for_selector(
                'div[aria-label="Chat list"], [data-testid="chat-list"], #pane-side',
                timeout=_LOGIN_TIMEOUT_MS)
            log.info("[%s] logged in - session saved to %s", name, profile_dir(name))
            ok = True
        except PWTimeout:
            log.warning("[%s] timed out waiting for QR scan", name)
            ok = False
        time.sleep(1.5)   # let WA flush the session to disk before we close
        ctx.close()
    return ok


def account_status(name: str) -> str:
    """Quick headless probe: 'logged_in' | 'logged_out'."""
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir(name)), headless=True, locale="en",
            args=["--disable-blink-features=AutomationControlled"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto("https://web.whatsapp.com/", timeout=60_000)
            state = "logged_in" if _is_logged_in(page) else "logged_out"
        finally:
            ctx.close()
    return state


# -- batch verification with account rotation + daily cap -----------------------
def verify_places(
    store: Store,
    rows: list[dict[str, Any]],
    # (place_key, status, resolved_number|None) - the third arg has been passed since
    # 4e00a3a; the annotation lagged behind it, which is how a 2-arg callback slipped in.
    on_progress: Callable[[str, str, str | None], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    job_id: int | None = None,
) -> dict[str, int]:
    """Verify each row's number against WhatsApp. Writes wa_verified per place.

    Rotates across enabled accounts, respects each account's daily cap, paces checks.
    Returns counts: {yes, no, unknown, checked, capped, no_number}.
    """
    on_progress = on_progress or (lambda pk, s, num=None: None)
    should_stop = should_stop or (lambda: False)
    cap = settings.wa_daily_cap
    today = date.today().isoformat()
    counts = {"yes": 0, "no": 0, "unknown": 0, "checked": 0, "capped": 0, "no_number": 0}

    accounts = [a for a in store.list_wa_accounts() if not a["disabled"]]
    if not accounts:
        raise WaNotLoggedIn("no WhatsApp accounts - run `python -m webscraper wa-login <name>` first")

    open_ctx: dict[str, Any] = {}      # name -> (pw_ctx, page); all closed in `finally`
    relaunchers: dict[str, Relauncher] = {}   # name -> its own relaunch budget
    pw = sync_playwright().start()

    def _relaunch(name: str, where: str) -> bool:
        """Rebuild `name`'s dead browser and refresh its handle. False once its cap is spent.

        The rebuild reuses data/wa-profiles/<name>/ because the WhatsApp session lives in
        that profile (IndexedDB, not cookies) - a fresh dir comes back as an unlinked
        device demanding a QR, i.e. it would silently kill a perfectly good account.
        """
        rl = relaunchers.get(name)
        if rl is None:
            return False
        try:
            if not rl.recover(where):
                return False
        except WaNotLoggedIn:
            open_ctx.pop(name, None)      # relaunched profile came back unlinked
            raise
        open_ctx[name] = rl.current
        return True

    def _check(name: str, num: str) -> str:
        """One number on one account, retried on a fresh browser if Chrome dies.

        A relaunch is NOT another check: `bump_wa_account` and the counters run once per
        number in the caller, so a crash can never eat into the daily cap. Pacing is
        untouched too - the caller's randomised sleep still happens once per number.
        """
        while True:
            page = open_ctx[name][1]
            try:
                page.goto(WA_SEND.format(num=num), timeout=60_000, wait_until="domcontentloaded")
                st = _decide(page)
                _dismiss_popup(page)
                return st
            except PWTimeout:                 # subclass of PWError - must stay above it
                return "unknown"
            except PWError as e:
                # maps.py has had this since job #3 died mid-feed with TargetClosedError;
                # the lanes split (2026-08-23) gave WhatsApp its own long-lived browser
                # and no recovery at all, so one dead Chrome took the entire lane with it.
                if not is_closed(e) or not _relaunch(name, f"number {num}"):
                    raise
                # loop: retry this ONE number on the relaunched browser.

    try:
        for r in rows:
            if should_stop():
                break
            # A already-decided number is final — never re-check (defensive; callers filter).
            if r.get("wa_verified") in ("yes", "no"):
                continue
            num = _e164_digits(r.get("phone"), r.get("whatsapp_number"), r.get("country"))
            pk = r["place_key"]
            if not num:
                counts["no_number"] += 1
                if job_id is not None:
                    store.set_wa_verify(job_id, pk, "unknown", None)
                on_progress(pk, "unknown", None)
                continue

            name = store.pick_wa_account(cap, today)
            if name is None:
                counts["capped"] += 1
                log.info("all accounts hit the daily cap (%d) - stopping; re-run tomorrow", cap)
                break

            page = _ensure_session(pw, open_ctx, relaunchers, name)
            if page is None:      # account logged out — disable it and try the next row
                store.conn.execute("UPDATE wa_accounts SET disabled=1 WHERE name=?", (name,))
                store.conn.commit()
                log.warning("[%s] logged out - disabled; run wa-login to re-link", name)
                continue

            try:
                status = _check(name, num)
            except WaNotLoggedIn:
                # Session dropped mid-run, or a relaunch found the profile unlinked.
                store.conn.execute("UPDATE wa_accounts SET disabled=1 WHERE name=?", (name,))
                store.conn.commit()
                continue

            store.bump_wa_account(name, today)
            counts[status] += 1
            counts["checked"] += 1
            # Bare digits were only ever for the send URL; everything persisted or reported
            # carries the '+' (directive 2026-08-23). plus() is idempotent. The cloud path
            # (job_id=None) never touches set_wa_verify - agent.py writes the callback's
            # number straight into whatsapp_number - so it has to be +'d here, not in store.
            e164 = plus(num)
            if job_id is not None:
                # prior_source flows through untouched: 'assumed_mobile' was retired on
                # 2026-08-23 in favour of 'unverified', and set_wa_verify treats BOTH as
                # candidates to clear on a 'no', so old and new rows behave the same.
                store.set_wa_verify(job_id, pk, status, name,
                                    wa_number=e164, prior_source=r.get("whatsapp_source"))
            on_progress(pk, status, e164)
            time.sleep(random.uniform(settings.wa_delay_min, settings.wa_delay_max))
    finally:
        for pw_ctx, _ in open_ctx.values():
            try:
                pw_ctx.close()
            except Exception:  # noqa: BLE001
                pass
        pw.stop()
    return counts


def _ensure_session(pw, open_ctx: dict[str, Any],
                    relaunchers: dict[str, Relauncher] | None, name: str) -> Page | None:
    """Return a live, logged-in page for `name`, launching its profile once. None if logged out.

    The launch closure is handed to a Relauncher so a later crash can rebuild *exactly*
    this context - same persistent profile dir, same headless flag - without the caller
    needing to know how the browser was built. One Relauncher per account: a flaky account
    must not spend the relaunch budget of the others.
    """
    if name in open_ctx:
        return open_ctx[name][1]

    def _open() -> tuple[Any, Page]:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir(name)), headless=settings.wa_verify_headless, locale="en",
            viewport={"width": 1100, "height": 820},
            args=["--disable-blink-features=AutomationControlled"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://web.whatsapp.com/", timeout=60_000)
        if not _is_logged_in(page):
            # Raised, not returned: a relaunch happens deep inside `_check`, and this is
            # its only way to report "profile came back unlinked" through Relauncher.open().
            ctx.close()
            raise WaNotLoggedIn(f"[{name}] profile has no live WhatsApp Web session")
        return ctx, page

    rl = Relauncher(_open, on_restart=lambda where, n: log.warning(
        "[%s] WhatsApp browser died during %s - relaunching from %s (%d/%d)",
        name, where, profile_dir(name), n, MAX_RELAUNCH))
    try:
        ctx, page = rl.open()
    except WaNotLoggedIn:
        return None
    if relaunchers is not None:
        relaunchers[name] = rl
    open_ctx[name] = (ctx, page)
    return page
