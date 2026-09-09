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

import re

import logging
import random
import time
from datetime import date
from typing import Any, Callable

from playwright.sync_api import Error as PWError, Page, TimeoutError as PWTimeout, sync_playwright

from webscraper.browser_recovery import (MAX_RELAUNCH, RESTORE_BUBBLE_ARGS, Relauncher,
                                         is_closed, mark_profile_clean)
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
    """Chat list present (logged in) vs the QR / link-device landing (not).

    W67: a page that renders NEITHER within the window is a third thing — WhatsApp Web
    stuck on its own splash, which is what an unlinked-and-half-broken profile looks
    like. It is logged the moment it happens, because from the outside it is
    indistinguishable from "slow", and the Mac spent an afternoon that way with a window
    open on screen and nobody able to say why.
    """
    try:
        page.wait_for_selector(
            'div[aria-label="Chat list"], [data-testid="chat-list"], '
            'canvas[aria-label*="Scan"], [data-testid="qrcode"], div[data-ref]',
            timeout=40_000)
    except PWTimeout:
        log.warning("WhatsApp Web never got past its loading screen in 40s — the profile's "
                    "local store is likely unusable; delete it and link again")
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


#: W63 — how long WhatsApp Web gets to render anything at all (splash → QR or chat list)
#: before the human's scan window starts counting. It is not the scan time; it is the
#: client's own boot, which is what a "very slow" session actually is.
_BOOT_TIMEOUT_MS = 90_000

# -- login (headed, one-time per account) ---------------------------------------
#: W68 — the accounts a `wa-login` is holding a window open for, right now.
#:
#: The WhatsApp LANE and the login command are two threads of one process reaching for
#: the same persistent Chrome profile. The lane gives up in milliseconds when there is no
#: session — which is exactly when someone is most likely to be linking one — and since
#: W67 it also reaped the profile's browser on its way out. That killed the very window
#: the user was scanning: "Page.wait_for_selector: Target page, context or browser has
#: been closed" on the Mac, 2026-09-08.
#:
#: While a name is in here, nothing else opens, probes or reaps that profile.
_LOGIN_ACTIVE: set[str] = set()


def login_in_progress(name: str | None = None) -> bool:
    """True while a wa-login window is open — for `name`, or for any account."""
    return bool(_LOGIN_ACTIVE) if name is None else name in _LOGIN_ACTIVE


_ACCOUNT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,29}$")


def rename_account(old: str, new: str, *, busy: bool = False, store: "Store | None" = None) -> tuple[bool, str]:
    """W80: rename a WhatsApp account on this machine — its profile directory and its
    store row — so the CRM can call a number what it is ("sales", "hardik-personal")
    instead of main/spare1. Refused while anything could be holding the profile: a login
    window on either name, or a job in flight (`busy`), since a Chrome with the old
    directory open would keep writing into a folder that no longer exists."""
    old, new = (old or "").strip(), (new or "").strip()
    if not old or not new:
        return False, "usage: <old>><new>"
    if not _ACCOUNT_RE.match(new):
        return False, f"bad name {new!r} — lowercase letters, digits, - and _ only (max 30)"
    if old == new:
        return False, "same name"
    if busy:
        return False, "a job is running on this machine — stop it, then rename"
    if login_in_progress(old) or login_in_progress(new):
        return False, "a WhatsApp login window is open — finish or close it first"
    # Raw paths: profile_dir() creates the folder it names, which would make every
    # target look taken.
    src, dst = settings.wa_profiles_dir / old, settings.wa_profiles_dir / new
    if dst.exists():
        return False, f"{new} already exists on this machine"
    from webscraper.store import Store as _Store
    st = store or _Store()
    known = {a["name"] for a in st.list_wa_accounts()}
    if old not in known and not src.exists():
        return False, f"{old} is not an account on this machine"
    if new in known:
        return False, f"{new} already exists on this machine"
    if src.exists():
        try:
            from webscraper.browser_recovery import kill_profile_holder
            kill_profile_holder(src, "wa rename")
        except Exception:                                         # noqa: BLE001
            pass
        src.rename(dst)
    if old in known:
        st.rename_wa_account(old, new)
    else:
        st.add_wa_account(new)
    log.info("[%s] renamed to %s", old, new)
    return True, f"renamed {old} → {new}"


def reset_account(name: str, *, busy: bool = False) -> tuple[bool, str]:
    """W91 (CRM T528): forget a WhatsApp account on this machine — evict any Chrome holding
    its profile, delete the profile directory and the store row — so the next wa-login
    starts from a clean QR. Refused while a job or a login could be using the profile."""
    name = (name or "").strip()
    if not name or not _ACCOUNT_RE.match(name):
        return False, f"bad name {name!r}"
    if busy:
        return False, "a job is running on this machine — stop it, then reset"
    if login_in_progress(name):
        return False, "a login window is open for this account — close it first"
    import shutil
    d = settings.wa_profiles_dir / name
    if d.exists():
        try:
            from webscraper.browser_recovery import kill_profile_holder
            kill_profile_holder(d, "wa reset")
        except Exception:                                         # noqa: BLE001
            pass
        shutil.rmtree(d, ignore_errors=True)
    try:
        Store().remove_wa_account(name)
    except Exception:                                             # noqa: BLE001
        pass
    log.info("[%s] WhatsApp profile reset — next wa-login starts from a fresh QR", name)
    return True, f"reset {name} — start a new session to link it again"


def login(name: str) -> bool:
    """Open WhatsApp Web headed; wait for the QR to be scanned. Returns True on success.

    W70: a profile whose WhatsApp Web client never gets past the splash is wiped and
    relaunched once. The ASUS spent two whole windows on that screen (2026-09-08) and
    never saw a QR; a fresh profile boots to the QR in seconds. A profile that shows
    the chat list is linked and is never touched. Only a profile that shows NOTHING
    within the boot window is treated as broken — and nothing linked ever looks like
    that.
    """
    Store().add_wa_account(name)
    _LOGIN_ACTIVE.add(name)
    ok = False
    try:
        with sync_playwright() as pw:
            booted = _login_attempt(pw, name)
            if booted is None and _chrome_channel():
                # W91: "never rendered" can be the BROWSER, not the profile — the Mac's
                # `main` was wiped three times in an hour (2026-09-09) by this rule while
                # the installed Chrome simply would not paint WhatsApp Web. Try the
                # bundled Chromium once before destroying a possibly-linked session.
                log.warning("[%s] WhatsApp Web never rendered with the installed Chrome — retrying with bundled Chromium", name)
                booted = _login_attempt(pw, name, browser={})
            if booted is None:
                import shutil
                log.warning("[%s] WhatsApp Web never rendered on this profile — wiping it and "
                            "starting fresh", name)
                shutil.rmtree(profile_dir(name), ignore_errors=True)
                booted = _login_attempt(pw, name)
            ok = bool(booted)
    finally:
        _LOGIN_ACTIVE.discard(name)                                # W68
    return ok


def _login_attempt(pw, name: str, browser: dict[str, Any] | None = None) -> bool | None:
    """One headed login window. True = linked, False = QR shown but not scanned in time,
    None = the client never rendered anything at all (the profile is the problem).
    `browser` overrides the launch channel (W91: {} = bundled Chromium)."""
    mark_profile_clean(profile_dir(name))
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir(name)), headless=False, locale="en",
        **(_chrome_channel() if browser is None else browser),
        viewport={"width": 1100, "height": 820},
        args=["--disable-blink-features=AutomationControlled", *RESTORE_BUBBLE_ARGS])
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://web.whatsapp.com/", timeout=60_000)
        # W63: the scan window starts once the page has actually rendered — QR or chat
        # list — not the moment the tab opened; WhatsApp Web's own boot took the whole
        # window on the ASUS before, and no QR was ever shown.
        try:
            page.wait_for_selector(
                'canvas[aria-label*="Scan"], [data-testid="qrcode"], div[data-ref], '
                'div[aria-label="Chat list"], [data-testid="chat-list"], #pane-side',
                timeout=_BOOT_TIMEOUT_MS)
        except PWTimeout:
            log.warning("[%s] WhatsApp Web is still on its loading screen after %ds",
                        name, _BOOT_TIMEOUT_MS // 1000)
            return None
        log.info("[%s] scan the QR in the window (2 min)...", name)
        try:
            page.wait_for_selector(
                'div[aria-label="Chat list"], [data-testid="chat-list"], #pane-side',
                timeout=_LOGIN_TIMEOUT_MS)
            log.info("[%s] logged in - session saved to %s", name, profile_dir(name))
            Store().set_wa_status(name, "logged_in")               # W64
            time.sleep(1.5)   # let WA flush the session to disk before we close
            return True
        except PWTimeout:
            log.warning("[%s] timed out waiting for QR scan", name)
            Store().set_wa_status(name, "logged_out")              # W64
            return False
    finally:
        # T336: a goto that throws (offline, DNS hiccup) must not leak this window —
        # it stayed open at about:blank until the process was killed by hand.
        ctx.close()


def account_status(name: str) -> str:
    """Probe a profile: 'logged_in' | 'logged_out' | 'unknown'.

    W65: 'unknown' matters. `_is_logged_in` returns False both when WhatsApp shows the
    QR (really logged out) and when the page never rendered at all (slow client, the
    profile already open in another Chrome, no network). Recording the second as
    'logged_out' is how a working machine ends up marked dead — the same misread the
    `disabled` flag was once set by. Only a page that actually showed the link-device
    screen counts as logged out; anything else leaves the last known answer alone.
    """
    mark_profile_clean(profile_dir(name))
    with sync_playwright() as pw:
        # W88: the installed Chrome in new-headless mode — headless CHROMIUM never rendered
        # WhatsApp Web on these profiles, so this probe answered "unknown" after a 40 s
        # wait on every job boundary (the T499 gap). Same launch the headless verify mode
        # uses, which answered in ~5 s.
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir(name)), locale="en",
            **_launch_kwargs("headless"))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto("https://web.whatsapp.com/", timeout=60_000)
            if _is_logged_in(page):
                state = "logged_in"
            elif page.locator('canvas[aria-label*="Scan"], [data-testid="qrcode"], div[data-ref]').count():
                state = "logged_out"        # the link-device screen: it really is unlinked
            else:
                state = "unknown"           # never rendered — say nothing rather than lie
            if state != "unknown":
                Store().set_wa_status(name, state)                 # W64
        finally:
            ctx.close()
    return state


# -- batch verification with account rotation + daily cap -----------------------
def verify_places(
    store: Store,
    rows: list[dict[str, Any]],
    # (place_key, status, resolved_number|None, source|None). W26 added the 4th arg —
    # which of the place's numbers this verdict is about (maps | wa_link | site).
    on_progress: Callable[..., None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    job_id: int | None = None,
    # W59: this job's own window choice for WhatsApp Web. None = the agent's
    # WA_VERIFY_HEADLESS setting, which is what every run used before.
    headless: bool | None = None,
    # W76: run every number on THIS account instead of rotating. The WhatsApp lane
    # splits a batch across linked accounts and runs the slices at once; each slice
    # pins its own account so two threads never share one Chrome profile.
    account: str | None = None,
) -> dict[str, int]:
    """Verify numbers against WhatsApp, one verdict per NUMBER (W26).

    A row carrying `number` (+ `source`) — what `Store.pending_wa_verify` hands the lane —
    is checked on that one number. A bare place row (CLI, the CRM re-verify) expands to
    every distinct candidate via `store.wa_candidates`: the Maps phone, a WhatsApp link,
    the website's own numbers. With `job_id` each verdict lands in `wa_checks` and the
    place-level `wa_verified` / `whatsapp_number` are re-derived (`record_wa_check`).

    Rotates across enabled accounts, respects each account's daily cap, paces checks.
    Returns counts: {yes, no, unknown, checked, capped, no_number}.
    """
    from webscraper.store import wa_candidates
    on_progress = on_progress or (lambda pk, s, num=None, source=None: None)
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
        nav_retries = 0
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
                if is_closed(e):
                    # maps.py has had this since job #3 died mid-feed with TargetClosedError;
                    # the lanes split (2026-08-23) gave WhatsApp its own long-lived browser
                    # and no recovery at all, so one dead Chrome took the entire lane with it.
                    if not _relaunch(name, f"number {num}"):
                        raise
                    continue
                # T338: a plain navigation failure (net::ERR_CONNECTION_CLOSED/RESET/…) is
                # NOT "the browser died" — `is_closed()` correctly says no, but before this
                # fix that meant the error was re-raised anyway and killed the whole lane
                # (screenshot: 2h46m of good checks thrown away by one flaky request). The
                # browser and page are still alive here, so just retry navigation in place.
                if nav_retries >= 2:
                    log.warning("[%s] number %s: navigation kept failing (%s) - skipping",
                                name, num, e)
                    return "unknown"
                nav_retries += 1
                time.sleep(3.0)
                # loop: retry this ONE number on the relaunched browser.

    # Expand to (row, bare digits, source) — one entry per number to check.
    targets: list[tuple[dict[str, Any], str, str]] = []
    for r in rows:
        pk = r["place_key"]
        if r.get("number"):
            d = "".join(ch for ch in str(r["number"]) if ch.isdigit())
            if 8 <= len(d) <= 15:
                targets.append((r, d, str(r.get("source") or "maps")))
            continue
        cands = wa_candidates(r)
        if not cands:
            # Legacy single-number fallback covers a row whose phone did not parse for its
            # region but is still a plausible bare number.
            legacy = _e164_digits(r.get("phone"), r.get("whatsapp_number"), r.get("country"))
            if legacy:
                cands = [(f"+{legacy}", "maps")]
        if not cands:
            counts["no_number"] += 1
            if job_id is not None:
                store.set_wa_verify(job_id, pk, "unknown", None)
            on_progress(pk, "unknown", None, None)
            continue
        for e164, src in cands:
            targets.append((r, e164.lstrip("+"), src))

    try:
        for r, num, source in targets:
            if should_stop():
                break
            pk = r["place_key"]

            name = account if account else store.pick_wa_account(cap, today)
            if name is None:
                if cap > 0:
                    counts["capped"] += 1
                    log.info("all accounts hit the daily cap (%d) - stopping; re-run tomorrow", cap)
                else:
                    # No cap, so None means every account is disabled (logged out).
                    log.warning("no enabled WhatsApp account left - stopping")
                    if job_id is not None:
                        store.log(job_id, "whatsapp", "no enabled WhatsApp account left — run wa-login", "error")
                break

            page = _ensure_session(pw, open_ctx, relaunchers, name, headless)
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
                # Per-number verdict; the place-level wa_verified / whatsapp_number are
                # re-derived from every verdict so far (any yes → yes, all no → no).
                store.record_wa_check(job_id, pk, e164, source, status, name)
            on_progress(pk, status, e164, source)
            lo, hi = wa_delay_range()
            time.sleep(random.uniform(lo, hi))
    finally:
        for pw_ctx, _ in open_ctx.values():
            try:
                pw_ctx.close()
            except Exception:  # noqa: BLE001
                pass
        pw.stop()
    return counts


def _chrome_channel() -> dict[str, Any]:
    """W90: every WhatsApp launch uses the SAME browser. The verify modes and the probe used
    the installed Chrome (152); login used bundled Chromium (145). Chrome upgrades a
    profile's databases on open and an older build then refuses it — Re-link died with
    "Target page, context or browser has been closed" once a profile had been through a
    headless run (1 - PC, 2026-09-09). Installed Chrome when present, bundled Chromium
    only on a machine that has none."""
    try:
        from webscraper.healthcheck import chrome_path
        path = chrome_path()
    except Exception:                                             # noqa: BLE001
        path = None
    return {"channel": "chrome"} if path else {}


def wa_delay_range() -> tuple[float, float]:
    """W89 (CRM T521): the random pause between two numbers on one session, in seconds.

    Per machine from the CRM Systems card (lead_gen_settings `wa_delay__<device>`, pushed
    as env WA_DELAY__<DEVICE>, value "<min>-<max>" e.g. "1.5-4"), or WA_DELAY in .env;
    else the WA_VERIFY_DELAY_MIN/MAX settings (default 1.5-4 since 1.6.4, was 3-8).
    Read at call time on purpose: the agent pushes cloud config into os.environ AFTER
    `settings` was built, so a dataclass default would never see it. Floor 0.5 s and
    a non-inverted range, whatever the value says.
    """
    import os
    try:
        from webscraper.agent import DEVICE_NAME
    except Exception:                                             # noqa: BLE001
        DEVICE_NAME = ""
    raw = (os.getenv(f"WA_DELAY__{DEVICE_NAME.upper()}") if DEVICE_NAME else None) or os.getenv("WA_DELAY")
    lo, hi = settings.wa_delay_min, settings.wa_delay_max
    if raw:
        parts = [x.strip() for x in str(raw).replace("–", "-").split("-") if x.strip()]
        try:
            if len(parts) == 2:
                lo, hi = float(parts[0]), float(parts[1])
            elif len(parts) == 1:
                lo = hi = float(parts[0])
        except ValueError:
            pass
    lo = max(0.5, lo)
    hi = max(lo, hi)
    return lo, hi


def wa_window_mode() -> str:
    """W86 (CRM T521): how the verify session's Chrome runs on THIS machine.

    visible  — a normal window (W65 default: a quietly unlinked session is at least seen)
    hidden   — a real headed Chrome parked far off-screen: invisible to the user, identical
               to Meta. Probed 2026-09-09 on the PC's spare1 profile: chat list in 6 s.
    headless — real Chrome `--headless=new` (NOT headless Chromium, which WhatsApp Web
               refuses to render on these profiles — the W65 lesson): chat list in 5 s.
    Set per machine on the CRM Systems card (lead_gen_settings `wa_window__<device>`,
    pushed as env WA_WINDOW__<DEVICE>), or WA_WINDOW in .env as a local override.
    """
    import os
    try:
        from webscraper.agent import DEVICE_NAME
    except Exception:                                             # noqa: BLE001
        DEVICE_NAME = ""
    raw = (os.getenv(f"WA_WINDOW__{DEVICE_NAME.upper()}") if DEVICE_NAME else None) \
        or os.getenv("WA_WINDOW") or "visible"
    raw = str(raw).strip().lower()
    return raw if raw in ("visible", "hidden", "headless") else "visible"


_WINDOW_FELL_BACK: set[str] = set()


def _launch_kwargs(mode: str) -> dict[str, Any]:
    """Playwright launch options for a mode. Hidden/headless use the installed Chrome —
    that is what made both render where headless Chromium never did."""
    base = ["--disable-blink-features=AutomationControlled", *RESTORE_BUBBLE_ARGS]
    ch = _chrome_channel()
    if mode == "headless" and not ch:
        # Bundled Chromium never renders WhatsApp Web headless (W65): without an installed
        # Chrome, "headless" degrades to hidden, which at least keeps working.
        log.warning("no installed Chrome — WhatsApp 'headless' runs as 'hidden' on this machine")
        mode = "hidden"
    if mode == "hidden":
        return {"headless": False, **ch,
                "args": base + ["--window-position=-32000,-32000", "--window-size=1100,820"]}
    if mode == "headless":
        return {"headless": False, **ch, "args": base + ["--headless=new"]}
    return {"headless": False, **ch, "args": base}


def _ensure_session(pw, open_ctx: dict[str, Any],
                    relaunchers: dict[str, Relauncher] | None, name: str,
                    headless: bool | None = None) -> Page | None:
    """Return a live, logged-in page for `name`, launching its profile once. None if logged out.

    The launch closure is handed to a Relauncher so a later crash can rebuild *exactly*
    this context - same persistent profile dir, same headless flag - without the caller
    needing to know how the browser was built. One Relauncher per account: a flaky account
    must not spend the relaunch budget of the others.
    """
    if name in open_ctx:
        return open_ctx[name][1]

    # W68: someone is linking this account right now. Opening the same persistent profile
    # from here would either fail on its lock or fight the window they are scanning.
    if login_in_progress(name):
        raise WaNotLoggedIn(f"[{name}] a WhatsApp login is open on this machine — waiting for it")

    def _open() -> tuple[Any, Page]:
        mark_profile_clean(profile_dir(name))
        # W65 (user directive 2026-09-08) made the window always visible; W86 (T521) makes
        # that the per-machine DEFAULT and adds hidden / headless (real Chrome) — a mode
        # that failed to render once on this account falls back to visible for the run.
        mode = "visible" if name in _WINDOW_FELL_BACK else wa_window_mode()
        if mode != "visible":
            log.info("[%s] WhatsApp window mode: %s", name, mode)
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir(name)),
            locale="en", viewport={"width": 1100, "height": 820},
            **_launch_kwargs(mode))
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            if mode == "hidden":
                # Off-screen is honoured (probe: bounds stay at -32000), but the window
                # still has a taskbar entry; minimising it too keeps it out of Alt-Tab.
                # Best effort — a failure here must never cost the session.
                try:
                    cdp = ctx.new_cdp_session(page)
                    wid = cdp.send("Browser.getWindowForTarget")["windowId"]
                    cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "minimized"}})
                except Exception:                                 # noqa: BLE001
                    log.debug("[%s] could not minimise the hidden window", name, exc_info=True)
            # W60: retry the first navigation instead of letting one slow load end the lane.
            # `Page.goto: Timeout 60000ms exceeded` was the recorded end reason on six jobs
            # (#5797 stopped at 109 of 497 numbers, #5798 at 54 of 272), and the run reported
            # itself "done" over the top of it. WhatsApp Web is a heavy first paint on a cold
            # profile, so the timeout is generous and `domcontentloaded` is enough — the
            # login probe below waits for what actually matters.
            last: Exception | None = None
            for attempt in range(3):
                try:
                    page.goto("https://web.whatsapp.com/", timeout=90_000,
                              wait_until="domcontentloaded")
                    last = None
                    break
                except Exception as e:                            # noqa: BLE001
                    last = e
                    log.warning("[%s] WhatsApp Web did not load (attempt %d/3): %s",
                                name, attempt + 1, str(e).splitlines()[0])
                    time.sleep(3 * (attempt + 1))
            if last is not None:
                raise last
            if not _is_logged_in(page):
                # W86: a hidden / headless launch that shows NEITHER the chat list nor the
                # QR never rendered — that is the mode failing, not the account. Fall back
                # to a visible window for this account for the rest of the run instead of
                # calling a linked number "logged out" (the W65 misread).
                if mode != "visible" and not page.locator(
                        'canvas[aria-label*="Scan"], [data-testid="qrcode"], div[data-ref]').count():
                    _WINDOW_FELL_BACK.add(name)
                    log.warning("[%s] WhatsApp Web did not render in %s mode — retrying with a visible window", name, mode)
                    ctx.close()
                    return _open()
                # Raised, not returned: a relaunch happens deep inside `_check`, and this is
                # its only way to report "profile came back unlinked" through Relauncher.open().
                Store().set_wa_status(name, "logged_out")      # W64
                raise WaNotLoggedIn(f"[{name}] profile has no live WhatsApp Web session")
            Store().set_wa_status(name, "logged_in")           # W64
            return ctx, page
        except BaseException:
            # T336: `ctx` is a real Chrome process the moment launch_persistent_context
            # returns. Before this, a goto that threw (offline, DNS hiccup, WA down) skipped
            # straight past `ctx.close()` and left the window sitting at about:blank forever
            # — the reported "opened a lot, lagged my Mac". Any failure here closes it.
            ctx.close()
            raise

    rl = Relauncher(_open, profile_dir=profile_dir(name), on_restart=lambda where, n: log.warning(
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
