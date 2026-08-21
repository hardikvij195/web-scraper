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

Ban risk is never zero - use a spare number, not your main business WhatsApp.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import date
from typing import Any, Callable

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from webscraper.config import settings
from webscraper.extractors import normalise_phone
from webscraper.store import Store

log = logging.getLogger("webscraper.wa_verify")

WA_SEND = "https://web.whatsapp.com/send?phone={num}&text&type=phone_number&app_absent=0"
_LOGIN_TIMEOUT_MS = 120_000     # QR scan grace
_CHECK_TIMEOUT_MS = 45_000      # per-number decision grace


class WaNotLoggedIn(RuntimeError):
    """The account's profile has no live WhatsApp Web session (needs `wa-login`)."""


def profile_dir(name: str):
    d = settings.wa_profiles_dir / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _e164_digits(phone: str | None, wa_number: str | None, country: str | None) -> str | None:
    """Best full international number (digits only, no +) for a place."""
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

    Valid number      -> WhatsApp opens the conversation (compose box appears).
    Invalid/not-on-WA -> a popup: "Phone number shared via url is invalid."
    """
    deadline = time.monotonic() + _CHECK_TIMEOUT_MS / 1000
    invalid_sel = (
        'div[role="dialog"]:has-text("invalid"), '
        'div[role="dialog"]:has-text("not on WhatsApp"), '
        '[data-testid="popup-contents"]:has-text("invalid")'
    )
    compose_sel = (
        'footer div[contenteditable="true"][data-tab], '
        '[data-testid="conversation-compose-box-input"], '
        'div[title="Type a message"]'
    )
    while time.monotonic() < deadline:
        if page.locator(invalid_sel).count():
            return "no"
        if page.locator(compose_sel).count():
            return "yes"
        # Session dropped mid-run (rare) — surface as not-logged-in so the caller stops.
        if page.locator('canvas[aria-label*="Scan"], [data-testid="qrcode"]').count():
            raise WaNotLoggedIn("session ended mid-run")
        page.wait_for_timeout(500)
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
    on_progress: Callable[[str, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    job_id: int | None = None,
) -> dict[str, int]:
    """Verify each row's number against WhatsApp. Writes wa_verified per place.

    Rotates across enabled accounts, respects each account's daily cap, paces checks.
    Returns counts: {yes, no, unknown, checked, capped, no_number}.
    """
    on_progress = on_progress or (lambda pk, s: None)
    should_stop = should_stop or (lambda: False)
    cap = settings.wa_daily_cap
    today = date.today().isoformat()
    counts = {"yes": 0, "no": 0, "unknown": 0, "checked": 0, "capped": 0, "no_number": 0}

    accounts = [a for a in store.list_wa_accounts() if not a["disabled"]]
    if not accounts:
        raise WaNotLoggedIn("no WhatsApp accounts - run `python -m webscraper wa-login <name>` first")

    open_ctx: dict[str, Any] = {}   # name -> (pw_ctx, page)
    pw = sync_playwright().start()
    try:
        for r in rows:
            if should_stop():
                break
            num = _e164_digits(r.get("phone"), r.get("whatsapp_number"), r.get("country"))
            pk = r["place_key"]
            if not num:
                counts["no_number"] += 1
                if job_id is not None:
                    store.set_wa_verify(job_id, pk, "unknown", None)
                on_progress(pk, "unknown")
                continue

            name = store.pick_wa_account(cap, today)
            if name is None:
                counts["capped"] += 1
                log.info("all accounts hit the daily cap (%d) - stopping; re-run tomorrow", cap)
                break

            page = _ensure_session(pw, open_ctx, name)
            if page is None:      # account logged out — disable it and try the next row
                store.conn.execute("UPDATE wa_accounts SET disabled=1 WHERE name=?", (name,))
                store.conn.commit()
                log.warning("[%s] logged out - disabled; run wa-login to re-link", name)
                continue

            try:
                page.goto(WA_SEND.format(num=num), timeout=60_000, wait_until="domcontentloaded")
                status = _decide(page)
                _dismiss_popup(page)
            except WaNotLoggedIn:
                store.conn.execute("UPDATE wa_accounts SET disabled=1 WHERE name=?", (name,))
                store.conn.commit()
                continue
            except PWTimeout:
                status = "unknown"

            store.bump_wa_account(name, today)
            counts[status] += 1
            counts["checked"] += 1
            if job_id is not None:
                store.set_wa_verify(job_id, pk, status, name)
            on_progress(pk, status)
            time.sleep(random.uniform(settings.wa_delay_min, settings.wa_delay_max))
    finally:
        for pw_ctx, _ in open_ctx.values():
            try:
                pw_ctx.close()
            except Exception:  # noqa: BLE001
                pass
        pw.stop()
    return counts


def _ensure_session(pw, open_ctx: dict[str, Any], name: str) -> Page | None:
    """Return a live, logged-in page for `name`, launching its profile once. None if logged out."""
    if name in open_ctx:
        return open_ctx[name][1]
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir(name)), headless=settings.wa_verify_headless, locale="en",
        viewport={"width": 1100, "height": 820},
        args=["--disable-blink-features=AutomationControlled"])
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://web.whatsapp.com/", timeout=60_000)
    if not _is_logged_in(page):
        ctx.close()
        return None
    open_ctx[name] = (ctx, page)
    return page
