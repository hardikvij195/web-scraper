"""Three concurrent lanes for one job: discovery → enrichment → WhatsApp.

Before this (2026-08-23) the worker ran four phases strictly in sequence against two
budgets, and WhatsApp — last in line and the slowest by design — always lost the race. Live
job #6 is the proof: `WhatsApp verification 2 / 30 numbers` labelled *done*, because
enrichment and AI research had spent the shared post-Maps budget and the lane exited after
two checks. Running the lanes at the same time removes the race instead of re-tuning it.

    Lane A  discovery    Maps Chrome ×2 since W26: a COLLECTOR tab tiles the search and
       │                 writes stub places rows (detail_status='pending') per tile; an
       │                 OPENER tab reads each panel and fills the row (detail_status='done')
       │  writes places rows with enrich_status='pending'
       ▼
    Lane B  enrichment   httpx site+socials, then the AI summary for that same lead
       │  writes enrich_status / research_status
       ▼
    Lane C  whatsapp     WhatsApp Web Chrome, existing pacing + per-account daily cap

**The `places` table is the queue.** Lanes hand each other nothing in memory: B selects
`enrich_status='pending'` rows whose panel has been read (`detail_status='done'`), C selects
NUMBERS without a verdict in `wa_checks` — the Maps phone the moment the opener writes it,
the website's numbers once enrichment has resolved (W26). That was chosen over a `queue.Queue` for two reasons —
it leaves `maps.py` alone, and it makes every lane restart-safe for free, which is what the
supervisor restart already depends on.

Each lane owns its **own `Store`**: `sqlite3` connections are not thread-safe. Lanes write
disjoint columns (`disc_*` / `enr_*` / `wa_*` and their own counters), which is what makes
three threads on one SQLite file safe here. Preserve that when adding a counter.

`max_minutes` caps **discovery only**. Enrichment and WhatsApp drain whatever discovery
found, however long that takes — the user's call, because a lead found at minute 29 is
worth nothing unverified.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from collections import deque
from typing import Any, Callable

from webscraper.config import settings
from webscraper.store import Store, now_iso

log = logging.getLogger("webscraper.lanes")

#: How long a downstream lane waits before re-checking its queue when it is empty but the
#: lane feeding it is still running. Two seconds is invisible next to a ~3.5 s/place scrape
#: and keeps the polling cost to nothing.
IDLE_POLL_SEC = 2.0

#: W136 (CRM T1015): a lane that holds a stage slot but has nothing to do (feeder still
#: running, nothing pending) gives the slot up at once when another job waits for it, and
#: after this long even when nobody does — so a lane between two batches does not churn.
IDLE_SLOT_SEC = 60.0
# W77: how long the WhatsApp lane waits for an open wa-login window before giving up —
# the QR window itself times out after 2 min, so this only ever waits out a real scan.
WA_LOGIN_WAIT_SEC = 240.0

#: W110: with no linked WhatsApp session the lane waits for a re-link instead of ending —
#: checked every WA_RELINK_POLL_SEC, for as long as discovery / enrichment still feed it
#: numbers, then WA_RELINK_GRACE_SEC more before it finally gives up.
WA_RELINK_POLL_SEC = 15.0
WA_RELINK_GRACE_SEC = 1800.0

#: T928 (2026-09-24): how often the wait re-announces itself while parked — comfortably
#: under agent.py's STALL_SEC (600s), so `job_logs` keeps growing and the stall watchdog
#: reads a known, narrated wait as alive instead of a hang. Before this fix the wait logged
#: once on entry and then nothing for up to WA_RELINK_GRACE_SEC (30 min): 18 jobs on ASUS,
#: DELL and MI (no linked WhatsApp account on any of them) were killed by the watchdog at
#: 607s of silence and reported to the CRM as `error`.
WA_RELINK_HEARTBEAT_SEC = 120.0

#: W137 (CRM T1016): WhatsApp Web stuck on "messages are downloading" on every load (MAC,
#: 2026-10-04: 34 'could not decide', 0 verdicts in an hour, websites waiting behind it). When a
#: slice decides nothing for that reason the lane parks (slot released) for WA_SYNC_PARK_SEC and
#: retries; after WA_SYNC_GIVE_UP parks in a row it ends with a readable error so the websites
#: lane stops waiting (W76) and the CRM shows the real problem.
#: W143 (CRM T1021): the per-ACCOUNT ladder in `wa_verify` (`WA_RESYNC_*`) now ends a re-sync loop —
#: episode 3 = browser recovery + one long wait, episode 4 = `needs_relink` — so this give-up is only
#: a backstop for a lane whose accounts keep alternating; it was the whole mechanism before (and reset
#: per job, which is how the Mac paused 92 times in a day).
WA_SYNC_PARK_SEC = 300.0
WA_SYNC_GIVE_UP = 12

#: W152 (CRM T1037, 2026-10-07): while the lane waits for a relink it re-PROBES every account the W143
#: ladder flagged `needs_relink` — on entry and every WA_RELINK_REPROBE_SEC (env, default 300) — with
#: `wa_verify.account_status` (one headless open, ~5 s when the link is fine). ASUS / MI / DELL were
#: flagged during the T1045 RAM thrash (the sync never finished because the MACHINE was frozen); after
#: the restart their sessions were fine, the owner's Start session showed the chat list with no QR,
#: yet three WhatsApp lanes had already waited 15 min each and ended `wa_no_session`. A probe that
#: sees the chat list stamps `logged_in` (which clears the flag — Store.set_wa_status) and the lane
#: resumes by itself. Skipped while a login window is open for that account or RAM is >= 80 %.
WA_RELINK_REPROBE_SEC = 300.0


def wa_relink_reprobe_sec() -> float:
    try:
        return max(0.0, float(os.getenv("WA_RELINK_REPROBE_SEC", str(WA_RELINK_REPROBE_SEC)) or WA_RELINK_REPROBE_SEC))
    except ValueError:
        return WA_RELINK_REPROBE_SEC


def _account_status(name: str) -> str:
    """Indirection so tests swap the probe without touching wa_verify / Playwright."""
    from webscraper import wa_verify
    return wa_verify.account_status(name)


def _reprobe_flagged(lane: "Lane", store: Store) -> bool:
    """W152: probe every `needs_relink` account; True when one showed its chat list (link is fine)."""
    names = list(getattr(store, "flagged_wa_accounts", lambda: [])() or [])
    if not names:
        return False
    wa_verify = None
    try:
        from webscraper import wa_verify
        from webscraper.enrich import browser_fallback_allowed
        if not browser_fallback_allowed():                      # W151: no extra Chrome above 80 % RAM
            lane.note("WhatsApp relink re-probe skipped — memory is high on this machine; trying again later", "info")
            return False
    except Exception:                                             # noqa: BLE001 — no reading: probe anyway
        pass
    ok = False
    for name in names:
        try:
            if wa_verify is not None and wa_verify.login_in_progress(name):
                continue                                          # the owner is scanning right now
            state = _account_status(name)
        except Exception as e:                                    # noqa: BLE001 — a probe must never end the wait
            log.warning("[%s] relink re-probe failed: %s", name, str(e).splitlines()[0][:160])
            continue
        if state == "logged_in":
            lane.note(f"WhatsApp [{name}] is still linked — the NEEDS RELINK flag was a false alarm (WhatsApp Web "
                      "never finished syncing while this machine was under load); cleared it, WhatsApp lane resuming",
                      "info")
            ok = True
        else:
            lane.note(f"WhatsApp [{name}] re-probed: {str(state).replace('_', ' ')} — still needs a QR relink from "
                      "Lead Finder > Systems > WhatsApp login", "info")
    return ok


def _wait_for_relink(lane, store: Store, err: Exception):
    """W110: park the WhatsApp lane until an account on this machine is seen logged in again.

    True = a re-link happened, retry the batch · False = gave up (nothing else is coming and the
    grace ran out, or — T928 — no account was ever linked on this machine and there is nothing
    left to feed the lane) · R_STOPPED = the job was stopped. A finished login stamps
    `wa_accounts.status_at` (Store.set_wa_status), which is what this polls — never a browser."""
    # W135: while parked here the lane is legitimately idle — the agent's global stall
    # watchdog (`agent._restart_if_all_stalled`) must not read that as "nothing moves".
    RELINK_WAITERS.add(lane.job_id)
    try:
        return _wait_for_relink_inner(lane, store, err)
    finally:
        RELINK_WAITERS.discard(lane.job_id)


#: W135: job ids whose WhatsApp lane is parked in `_wait_for_relink` right now.
RELINK_WAITERS: set[int] = set()

#: W144 (CRM T1024): the relink wait is bounded. Tests swap the clock.
_relink_now = time.monotonic


def wa_no_session_give_up_sec() -> float:
    """W144: how long a WhatsApp lane waits for a linked account before it ends `wa_no_session`
    and lets the CRM move the job's WhatsApp pass to a machine that has one. Env
    `WA_NO_SESSION_GIVE_UP_SEC`, default 900; 0 = wait for ever (the pre-W144 W110 behaviour)."""
    try:
        return max(0.0, float(os.getenv("WA_NO_SESSION_GIVE_UP_SEC", "900") or "900"))
    except ValueError:
        return 900.0


def _wait_for_relink_inner(lane: "Lane", store: Store, err: Exception) -> bool | str:
    since = now_iso()
    # T928: this machine has literally no enabled WhatsApp account (the exact condition
    # `wa_verify.verify_places` raises "no WhatsApp accounts - run wa-login ..." on) — not an
    # account whose session merely dropped mid-run (W110/T601, where waiting out the full
    # grace for a human to re-link is the point). Once nothing else is coming, waiting a
    # further 30 minutes for a link that nobody is mid-way through making buys nothing.
    no_accounts_ever = not store.enabled_wa_accounts()
    lane.note(f"WhatsApp lane waiting — {err}. Link a WhatsApp account on this machine from the "
              "CRM and it carries on by itself", "warn")
    # W123: a parked lane must not hold the WhatsApp stage slot (W122) — on a machine with no
    # linked account every job's lane would otherwise sit here for enrichment + 30 min while the
    # next jobs' lanes queue behind it. Give the slot back while parked, take it again on resume.
    gate = STAGE_GATES.get(lane.key)
    if gate is not None:
        gate.release(lane.job_id)
    grace_end = None
    last_beat = time.monotonic()
    give_up = wa_no_session_give_up_sec()
    wait_t0 = _relink_now()
    reprobe = wa_relink_reprobe_sec()
    next_probe = wait_t0                                          # W152: first probe on entry
    probed_ok = False
    while True:
        if lane.stopped():
            return R_STOPPED
        if reprobe > 0 and not probed_ok and _relink_now() >= next_probe:
            next_probe = _relink_now() + reprobe
            probed_ok = _reprobe_flagged(lane, store)
        if probed_ok or store.wa_relinked_since(since):
            lane.note("WhatsApp account linked — WhatsApp lane resuming", "info")
            if gate is not None and not gate.acquire(lane.job_id, lane.stopped, lambda m: lane.note(m)):
                return R_STOPPED
            return True
        if give_up > 0 and _relink_now() - wait_t0 >= give_up:
            # W144 (CRM T1024): nobody linked an account here within the window — stop holding
            # the job's WhatsApp pass hostage to this machine. The lane ends `wa_no_session`,
            # the job ends `incomplete` with its WhatsApp leftover and the CRM auto-heal re-runs
            # that pass on a machine with a session.
            return R_WA_NO_SESSION
        if lane.ctl.enrichment_finished():
            if no_accounts_ever and not store.enabled_wa_accounts():
                # T928: discovery + enrichment are done, nothing more will ever feed this
                # lane a number to skip waiting for, and no account was ever linked here —
                # finish now instead of sitting out the 30-min grace. The job still ends
                # 'done'; the CRM's own trigger reclassifies it 'incomplete' because the
                # numbers this run found still have no wa_verified verdict.
                try:
                    from webscraper.agent import DEVICE_NAME as _device
                except Exception:                                    # noqa: BLE001
                    _device = "this device"
                lane.note(f'WhatsApp check skipped — no WhatsApp account linked on {_device}; '
                          're-run "WhatsApp verify" after linking', "warn")
                return False
            grace_end = grace_end or time.monotonic() + WA_RELINK_GRACE_SEC
            if time.monotonic() >= grace_end:
                return False                 # gave up: Lane.run's finally releases again (a no-op now)
        now = time.monotonic()
        if now - last_beat >= WA_RELINK_HEARTBEAT_SEC:
            lane.note("WhatsApp lane still waiting for a linked account — nothing to report yet", "info")
            last_beat = now
        time.sleep(WA_RELINK_POLL_SEC)


#: Enrichment gets its speed from concurrency inside `enrich_places`, so it takes a batch
#: rather than one lead at a time. Small enough that a lead reaches WhatsApp quickly.
ENRICH_BATCH = 10

#: W152: all-provider AI research failures needed (with >= 50 % of the lane's researched leads) before
#: the enrichment lane ends `error:AI research: …` instead of `completed`.
RESEARCH_ERROR_MIN_FAILED = 5

#: Reason tokens. `ok` is true only for the first two — see Store.OK_REASONS.
R_COMPLETED = "completed"          # ran out of work: the honest "done"
R_NO_TARGETS = "no_targets"        # nothing qualified for this lane
R_MAPS_CAP = "maps_cap"            # discovery hit max_minutes
R_STOPPED = "stopped"              # user pressed Stop
R_WA_CAP = "wa_daily_cap"          # per-account WhatsApp cap reached
R_WA_LOGIN = "wa_not_logged_in"    # no live WhatsApp Web session
R_WA_NO_SESSION = "wa_no_session"  # W144: waited WA_NO_SESSION_GIVE_UP_SEC for a session — the CRM moves the WA pass
R_DISABLED = "disabled"            # the job did not ask for this lane


class StageGate:
    """W122 (CRM T784/T786): a FIFO gate shared by ONE STAGE (enrichment or WhatsApp)
    across every job in flight at once, so "one job's enrichment/WhatsApp at a time" (the
    default, `slots=1`) is enforced fairly ACROSS jobs instead of by running one whole job
    at a time — while Google Maps discovery for the next job is free to start the moment
    the current job's discovery ends (see `Worker` in server.py, which serialises
    discovery itself; this gate only ever holds "enrichment"/"whatsapp").

    FIFO by arrival: a job that has been waiting longest gets the next free slot, never a
    job that only just asked. Waiting lanes poll once a second so `stopped()` (the Stop
    button) is noticed quickly; `on_wait` fires once immediately and then every 90 s so a
    lane stuck for a long time keeps logging (`agent.py::_fail_stalled` fails any job
    whose log has gone quiet for 120 s — a silent wait would look like a stall)."""

    NOTE_EVERY_SEC = 90.0
    POLL_SEC = 1.0

    def __init__(self, key: str, slots: int) -> None:
        self.key = key
        self.slots = max(1, int(slots))
        self._cond = threading.Condition()
        self._queue: "deque[int]" = deque()
        self._holders: set[int] = set()

    def acquire(self, job_id: int, stopped: Callable[[], bool], on_wait: Callable[[str], None]) -> bool:
        with self._cond:
            if job_id not in self._queue and job_id not in self._holders:
                self._queue.append(job_id)
        noted_at: float | None = None
        while True:
            with self._cond:
                if stopped():
                    if job_id in self._queue:
                        self._queue.remove(job_id)
                    self._cond.notify_all()
                    return False
                free = self.slots - len(self._holders)
                if free > 0 and job_id in self._queue and self._queue.index(job_id) < free:
                    self._queue.remove(job_id)
                    self._holders.add(job_id)
                    self._cond.notify_all()
                    return True
                holder = next(iter(self._holders), None)
            now = time.monotonic()
            if noted_at is None or now - noted_at >= self.NOTE_EVERY_SEC:
                on_wait(f"waiting for the {self.key} slot — held by job #{holder}")
                noted_at = now
            time.sleep(self.POLL_SEC)

    def could_take(self, job_id: int) -> bool:
        """W153: would `acquire(job_id)` return at once? (a free slot, and nobody ahead of it)."""
        with self._cond:
            if job_id in self._holders:
                return True
            free = self.slots - len(self._holders)
            return free > 0 and (job_id not in self._queue or self._queue.index(job_id) < free)

    def holder_ids(self) -> list[int]:
        with self._cond:
            return sorted(self._holders)

    def busy(self) -> int:
        """W144: jobs whose lane is active on this gate or queued for it (holders + queue). A lane
        that gave its slot back while idle (W136 `_slot_idle`) is in neither — it is not busy."""
        with self._cond:
            return len(self._holders) + len(self._queue)

    def free(self) -> int:
        """W144: slots a NEW job's lane could take right now."""
        with self._cond:
            return max(0, self.slots - len(self._holders) - len(self._queue))

    def release(self, job_id: int) -> None:
        with self._cond:
            self._holders.discard(job_id)
            if job_id in self._queue:
                self._queue.remove(job_id)
            self._cond.notify_all()

    def set_slots(self, slots: int) -> None:
        """W135: resize in place (keeps the queue and the holders) — a CRM setting change
        must never drop a lane that already holds a slot."""
        with self._cond:
            self.slots = max(1, int(slots))
            self._cond.notify_all()

    def holds(self, job_id: int) -> bool:
        """W136: does `job_id` hold one of this gate's slots right now?"""
        with self._cond:
            return job_id in self._holders

    def others_waiting(self, job_id: int) -> bool:
        """W135: is any OTHER job queued for this gate right now?"""
        with self._cond:
            return any(j != job_id for j in self._queue)

    def yield_slot(self, job_id: int, stopped: Callable[[], bool], on_wait: Callable[[str], None]) -> bool:
        """W135 (CRM T1011): give the slot up and re-queue at the BACK, so the jobs that
        waited get a turn (round-robin across jobs), then wait for it again. A holder that
        nobody waits on keeps its slot — the caller checks `others_waiting` first, but this
        is safe to call regardless. Returns False only when `stopped()` fired meanwhile."""
        with self._cond:
            if job_id not in self._holders:
                return True
            self._holders.discard(job_id)
            if job_id not in self._queue:
                self._queue.append(job_id)
            self._cond.notify_all()
        return self.acquire(job_id, stopped, on_wait)

    def waiting_on(self, job_id: int) -> dict | None:
        """W124: `{"behind": [holders], "position": n}` while `job_id` is queued for this gate,
        else None. Read by the agent's progress builder so a parked job still *changes*."""
        with self._cond:
            if job_id not in self._queue:
                return None
            return {"behind": sorted(self._holders), "position": self._queue.index(job_id) + 1}


def _device_env(name: str) -> str | None:
    """`<NAME>__<DEVICE>` from the environment (CRM `lead_gen_settings` keys are pushed
    upper-cased with the device suffix, e.g. `ENRICH_SLOTS__DELL`)."""
    try:
        from webscraper.agent import DEVICE_NAME
    except Exception:                                             # noqa: BLE001
        DEVICE_NAME = ""
    return os.getenv(f"{name}__{DEVICE_NAME.upper()}") if DEVICE_NAME else None


#: W153 (CRM T1047, owner 2026-10-07: "each lane 1 job should run on each system and then it should be
#: queued back — logically you cannot run 2 WA jobs at the same time; same for Google Maps and
#: webscraping"). ON by default: every stage gate has ONE slot (Maps already is one tab), lanes never
#: round-robin (W135 yield off), and a job whose only remaining lane is held by another job is PARKED —
#: it ends with `PARK_MSG` and the CRM re-queues it (resumed over the saved leads) instead of sitting
#: "running" on the machine behind that lane. `LANE_ONE_JOB_PER_LANE=0` restores the W135/W144 behaviour.
def one_job_per_lane() -> bool:
    return (os.getenv("LANE_ONE_JOB_PER_LANE", "1") or "1").strip().lower() not in ("0", "false", "no", "off")


PARK_MSG = ("parked by the lane rule (W153): the {lane} lane on this machine is busy with job #{holder} — "
            "re-queued, resumes when a {lane} lane is free")


def enrich_slots() -> int:
    """W135: jobs whose enrichment lane may run at once on this machine. CRM setting
    `enrich_slots__<device>` (default 2, clamped 1..3); `LANE_SLOTS_ENRICHMENT` in .env
    is the local override. W153: always 1 under the one-job-per-lane rule."""
    if one_job_per_lane():
        return 1
    raw = os.getenv("LANE_SLOTS_ENRICHMENT") or _device_env("ENRICH_SLOTS")
    try:
        n = int(raw) if raw else 2
    except ValueError:
        n = 2
    return max(1, min(3, n))


def wa_slots() -> int:
    """W135: jobs whose WhatsApp lane may run at once. CRM setting `wa_slots__<device>`
    (clamped >= 1); default = `_wa_parallel()` (min of `wa_parallel__<device>` and the
    linked accounts is applied by the lane itself per batch). W153: always 1 under the rule."""
    if one_job_per_lane():
        return 1
    raw = os.getenv("LANE_SLOTS_WHATSAPP") or _device_env("WA_SLOTS")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return max(1, _wa_parallel())


def _build_stage_gates() -> dict[str, StageGate]:
    return {
        "enrichment": StageGate("enrichment", enrich_slots()),
        "whatsapp": StageGate("whatsapp", wa_slots()),
    }


def apply_stage_slots() -> dict[str, int]:
    """W135: re-read the slot settings and resize the LIVE gates in place. Called by the
    agent after the one-time cloud config apply and after every W128 config refresh."""
    want = {"enrichment": enrich_slots(), "whatsapp": wa_slots()}
    for k, n in want.items():
        g = STAGE_GATES.get(k)
        if g is not None and g.slots != n:
            log.info("stage gate %s: %d -> %d slot(s) (W135)", k, g.slots, n)
            g.set_slots(n)
    return want


#: W135 time-slicing: a lane gives its stage slot back after this many units (businesses
#: enriched / numbers checked) or after YIELD_AFTER_SEC, whichever first — but only when
#: another job is actually queued for that gate.
YIELD_UNITS = {"enrichment": 20, "whatsapp": 25}
YIELD_AFTER_SEC = 300.0
#: W149 (CRM T1032): a lane this close to the end keeps its slot instead of yielding — with 8
#: WhatsApp-only jobs round-robining 25 numbers a turn on ASUS, a job with 25 numbers left waited
#: ~50 min per turn and nothing ever FINISHED (slots stayed held, the agents table read "waiting"
#: for hours). Finishing small tails first frees in-flight slots for queued work.
TAIL_KEEP_UNITS = {"enrichment": 40, "whatsapp": 50}


def tail_keep(key: str, remaining: int | None) -> bool:
    """True when a lane with `remaining` units left should finish without yielding (W149)."""
    return remaining is not None and 0 < remaining <= TAIL_KEEP_UNITS.get(key, 0)




#: enrich_error token -> (what it means, whether re-enriching could ever help). User
#: report 2026-09-04: "FAILED: dns" logged with no explanation. Matches the tokens
#: `enrich.py::transport_error`/`crawl_error`/`http_error` actually store — keep in sync
#: with those if a new one is added there.
_ENRICH_ERROR_EXPLAIN = {
    "dns": ("the domain does not resolve — the site is offline, the domain expired, or "
            "the URL Google Maps listed is wrong", "not fixable by retrying; check/replace the website URL"),
    "timeout": ("the site took too long to answer (slow host, or it silently dropped the request)",
                "worth a re-enrich later — can be a temporary slowdown"),
    "network": ("a connection-level failure of no recognised kind",
                "worth a re-enrich later — often transient"),
    "tls": ("the TLS handshake failed (expired / mismatched certificate, or a host that only speaks an old protocol)",
            "a headed re-run proceeds past certificate warnings like a person would; if it persists the site is broken for everyone"),
    "reset": ("the host dropped the connection mid-request — often a firewall that resets non-browser clients",
              "worth a re-enrich; the TLS-impersonation and browser tiers usually get past it"),
    "refused": ("nothing is listening on that host/port (site down, or the domain points at a dead server)",
                "retry later; if it keeps refusing the website is offline"),
    "cf_deny": ("Cloudflare Error 1020 / 1015 — the site owner's firewall rule denies this network outright (no challenge offered)",
                "no fingerprint or browser changes a static deny from the same IP; only a proxy (ENRICH_PROXIES) can"),
    "bad_url": ("the site redirected to a malformed address (a Location header no browser could follow either)",
                "not fixable by retrying; the site's own redirect is broken — check the website URL on Maps"),
    "no_pages": ("the site answered but the crawl came back with nothing usable",
                 "worth a re-enrich, ideally with 'Show window' so you can see what the page actually rendered"),
    "blocked": ("the site returned a block/deny page (not a Cloudflare one we recognise)",
                "unlikely to change on a plain retry — a proxy or a headed re-run may get past it"),
    "recaptcha": ("the site is gated behind a Google reCAPTCHA image challenge",
                  "cannot be solved automatically — no fix here, that lead's contact info has to come from elsewhere (Maps phone, etc.)"),
    "cf_non_interactive": ("Cloudflare showed a wall with nothing to click (JS challenge / fingerprint check)",
                           "a proxy sometimes helps; otherwise this site is out of reach for the crawler"),
    "cf_managed": ("Cloudflare's managed challenge page (may include a checkbox)",
                   "re-enrich with ENRICH_CF_CLICK on (default) already tries the checkbox — if it's still failing, a proxy may help"),
    "cf_interactive": ("Cloudflare's interactive Turnstile checkbox challenge",
                       "the crawler already tries to click it automatically — a repeat failure usually means a proxy is needed"),
    "cf_embedded": ("an embedded Cloudflare Turnstile widget inside the page, not a full-page wall",
                    "same as the other Cloudflare reasons — a proxy is the next thing to try"),
}


def _enrich_error_detail(token: str | None) -> str:
    """Turn a raw `enrich_error` token into a sentence a non-engineer can act on."""
    if not token:
        return "unknown"
    m = re.match(r"^http_(\d{3})$", token)
    if m:
        code = int(m.group(1))
        if code == 404:
            return "HTTP 404 — the page/site returned 'not found'. The URL Google Maps has on file for this business is likely dead or wrong; not something a retry fixes"
        if code == 403:
            return "HTTP 403 — the site actively blocked this request (bot protection). A proxy or a headed re-run sometimes gets past it"
        if code == 429:
            return "HTTP 429 — the site itself is rate-limiting us. Worth a re-enrich after a pause"
        if code == 503:
            return "HTTP 503 — the site (or something in front of it, e.g. Cloudflare) is temporarily unavailable. Worth a re-enrich later"
        if code >= 500:
            return f"HTTP {code} — the site's own server is erroring, not something on our end. Worth a re-enrich later"
        return f"HTTP {code} — the site rejected the request"
    info = _ENRICH_ERROR_EXPLAIN.get(token)
    if info:
        meaning, fix = info
        return f"{token} — {meaning}. {fix}"
    return token


def _enrich_line(r: dict, status: str, f: dict) -> str:
    """'Name · site → done via tls · 1 email, instagram, whatsapp' / '… → FAILED: dns — ... '."""
    name = (r.get("name") or r.get("place_key") or "?")[:50]
    site = (r.get("website") or "").strip()
    if status == "no_website":
        return f"{name} · no website listed on Google Maps — nothing to crawl"
    found = []
    if f.get("emails"):
        n = len(f["emails"]); found.append(f"{n} email{'s' if n != 1 else ''}")
    for k in ("instagram", "facebook", "linkedin", "twitter_x", "youtube", "tiktok"):
        if f.get(k):
            found.append(k.replace("twitter_x", "x"))
    if f.get("whatsapp_number"):
        found.append(f"whatsapp ({f.get('whatsapp_source') or '?'})")
    via = f" via {f['enrich_via']}" if f.get("enrich_via") else ""
    if status == "failed":
        return f"{name} · {site} → FAILED: {_enrich_error_detail(f.get('enrich_error'))}"
    tail = ", ".join(found) if found else "no contacts found"
    return f"{name} · {site} → {status}{via} · {tail}"


#: Where a checked number came from, as the log line says it (W26).
WA_SOURCE_LABEL = {"maps": "maps", "wa_link": "whatsapp link", "site": "website", "leads": "CRM lead"}


def _wa_line(r: dict, status: str, num: str | None, source: str | None = None) -> str:
    """'Name · +44… (maps|website) → ON WhatsApp ✓ / not on WhatsApp ✗'."""
    name = (r.get("name") or r.get("place_key") or "?")[:50]
    verdict = {"yes": "ON WhatsApp ✓", "no": "not on WhatsApp ✗",
               "unknown": "could not decide" if num else "no number to check"}.get(status, status)
    label = WA_SOURCE_LABEL.get(source or "", source)
    src = f" ({label})" if (num and label) else ""
    return f"{name} · {num or '—'}{src} → {verdict}"


class Lane(threading.Thread):
    """One lane. Owns its Store, records its own start/end/reason, and never lets an
    exception escape — a lane that dies must not take its siblings with it."""

    key: str = "lane"

    def __init__(self, job_id: int, job: dict[str, Any], ctl: "Pipeline") -> None:
        super().__init__(name=f"lane-{self.key}-{job_id}", daemon=True)
        self.job_id = job_id
        self.job = job
        self.ctl = ctl
        self.done = threading.Event()
        self.reason: str | None = None
        self.store: Store | None = None
        # Set by EnrichmentLane._research when most Gemini calls fail (quota, bad key):
        # the lane then ends `error:AI research: …` instead of a clean "completed" that
        # hides 50 leads with no summary/owner (job #17, 2026-08-26).
        self.research_error: str | None = None
        # W152: the verdict above is CUMULATIVE over the lane (AI calls that fell through every
        # provider vs leads researched), not per batch: with 1-4 leads per batch one transient
        # "could not reach the API" tripped `>= len // 2` and ended a 316/316 lane `error:AI research`
        # (MAC #22535 / #22556, MI x6, 2026-10-06 — nvidia 369 fails vs 4,484 ok that day).
        self._research_targets = 0
        self._research_ai_failed = 0

    # -- helpers ---------------------------------------------------------------------
    def note(self, message: str, level: str = "info") -> None:
        """One line into the job's log history AND the latest-message field."""
        if self.store:
            self.store.log(self.job_id, self.key, message, level)
            self.store.update_job(self.job_id, message=message)
        log.info("[%s#%s] %s", self.key, self.job_id, message)

    def stopped(self) -> bool:
        return bool(self.store and self.store.stop_requested(self.job_id))

    def _remaining(self) -> int | None:
        """Units this lane still has to process (W149 tail rule); None = unknown."""
        if not self.store:
            return None
        try:
            if self.key == "whatsapp":
                return int(self.store.count_wa_pending(self.job_id))
            if self.key == "enrichment":
                return int(self.store.count_pending_enrichment(self.job_id))
        except Exception:                                         # noqa: BLE001
            return None
        return None

    def _slice_done(self, n: int) -> bool:
        """W135 (CRM T1011): fair interleaving. Count `n` units into the current slice; once
        the slice is full (`YIELD_UNITS` / `YIELD_AFTER_SEC`) and another job is queued for
        this stage, give the slot back and re-queue at the back. Counters and ETA live in
        `jobs` and keep accumulating across slices. Returns False if the job was stopped
        while waiting for the slot again."""
        self._slice_n = getattr(self, "_slice_n", 0) + n
        t0 = getattr(self, "_slice_t0", None)
        if t0 is None:
            t0 = self._slice_t0 = time.monotonic()
        gate = STAGE_GATES.get(self.key)
        if gate is None:
            return True
        if one_job_per_lane():
            return True                                  # W153: one job runs a lane to its end, no turns
        if self._slice_n < YIELD_UNITS.get(self.key, 20) and time.monotonic() - t0 < YIELD_AFTER_SEC:
            return True
        done_in_turn = self._slice_n
        self._slice_n = 0
        self._slice_t0 = time.monotonic()
        if not gate.others_waiting(self.job_id):
            return True                                  # lone job: keep the slot
        if tail_keep(self.key, self._remaining()):
            return True                                  # W149: almost done — finish, free the job

        def _note_limited(m: str) -> None:
            now = time.monotonic()
            if now - getattr(self, "_yield_note_at", 0.0) >= StageGate.NOTE_EVERY_SEC:
                self._yield_note_at = now
                self.note(m)

        _note_limited(f"handing the {self.key} slot to the next job in line after {done_in_turn} "
                      f"in this turn — back in the queue")
        return gate.yield_slot(self.job_id, self.stopped, _note_limited)

    # -- W136 (CRM T1015): a slot is held only while there is work ----------------------
    #
    # DELL, 2026-10-03 16:53 UTC -> 2026-10-04: job #21540's enrichment lane held the
    # enrichment slot while parked on the "websites wait — WhatsApp first" loop; its
    # WhatsApp lane queued for the WhatsApp slot, held by job #21543's WhatsApp lane, which
    # sat idle (nothing pending) waiting for its enrichment feeder — queued behind #21540.
    # A four-way circular wait: nobody worked for 21 h, every lane kept logging a
    # healthy-looking "waiting for the … slot — held by job #N" line every 90 s, and both
    # watchdogs (`agent._fail_stalled`, `agent._restart_if_all_stalled`) counted those
    # lines as progress. The rule now: an idle lane does not own a slot. It releases it
    # (`_slot_idle`) and queues again like any other waiter the moment a batch shows up
    # (`_slot_resume`). This is also what lets one job's WhatsApp lane run while another
    # job's WhatsApp lane is merely waiting for numbers.
    _slot_parked: bool = False
    _idle_since: float | None = None

    def _slot_idle(self, why: str, force: bool = False) -> None:
        """Call on every poll of an idle loop. Releases the stage slot at once when another
        job is queued for it (or `force`), otherwise after IDLE_SLOT_SEC of idling."""
        gate = STAGE_GATES.get(self.key)
        if gate is None or not gate.holds(self.job_id):
            return
        now = time.monotonic()
        if self._idle_since is None:
            self._idle_since = now
        if not (force or gate.others_waiting(self.job_id) or now - self._idle_since >= IDLE_SLOT_SEC):
            return
        gate.release(self.job_id)
        self._slot_parked = True
        self._slice_n = 0
        self._slice_t0 = None
        self.note(f"{why} — handing the {self.key} slot to the next job in line meanwhile")

    # -- W153 (CRM T1047): one job per lane — a job that would only WAIT here is parked -------
    def _park_due(self, gate: "StageGate") -> bool:
        """True when this lane cannot take the gate now AND no other lane of this job is still
        working — the job would sit "running" on this machine doing nothing but queueing behind
        another job's lane. Parking it hands it back to the CRM (resumed over the saved leads)."""
        if not one_job_per_lane() or gate.could_take(self.job_id):
            return False
        other_done = getattr(self.ctl, "other_lanes_done", None)
        try:
            return bool(other_done(self)) if other_done is not None else False
        except Exception:                                         # noqa: BLE001
            return False

    def _park(self, gate: "StageGate") -> None:
        holders = gate.holder_ids()
        msg = PARK_MSG.format(lane=self.key, holder=holders[0] if holders else "?")
        log.warning("job #%s: %s", self.job_id, msg)
        try:
            if self.store:
                self.store.update_job(self.job_id, stop_requested=1, message=msg)
                self.store.log(self.job_id, "job", msg, "warn")
        except Exception:                                         # noqa: BLE001
            log.warning("job #%s: could not record the park", self.job_id, exc_info=True)

    def _acquire(self, gate: "StageGate") -> bool:
        """`gate.acquire` that also gives up — and parks the job — when `_park_due` becomes true
        while waiting. False = stopped or parked (the caller ends the lane R_STOPPED either way)."""
        parked = {"v": False}

        def stop_or_park() -> bool:
            if self.stopped():
                return True
            if self._park_due(gate):
                parked["v"] = True
                return True
            return False
        if gate.acquire(self.job_id, stop_or_park, lambda m: self.note(m)):
            return True
        if parked["v"] and not self.stopped():
            self._park(gate)
        return False

    def _slot_resume(self) -> bool:
        """Call once a batch is in hand. Takes a slot back if `_slot_idle` gave it up
        (FIFO behind whoever asked earlier). False only when the job was stopped meanwhile."""
        self._idle_since = None
        gate = STAGE_GATES.get(self.key)
        if gate is None or not self._slot_parked:
            return True
        if not self._acquire(gate):                              # W153: parks instead of waiting
            return False
        self._slot_parked = False
        self._slice_n = 0
        self._slice_t0 = time.monotonic()
        self.note(f"work arrived — {self.key} slot taken back")
        return True

    # -- thread body -----------------------------------------------------------------
    def run(self) -> None:
        # Built through the pipeline's factory rather than `Store()` directly so a test can
        # point the lanes at a temp DB — and so the "one connection per lane" rule stays
        # visible at the single place it is enforced.
        self.store = self.ctl.new_store()
        gate = STAGE_GATES.get(self.key)
        gate_acquired = False
        try:
            if not self.enabled():
                self.reason = R_DISABLED
                # Record it, and wipe any stamp left by an earlier pass of this job.
                # A re-run reuses the row, so yesterday's `scrape_started_at` would
                # otherwise make a switched-off lane read as still running.
                self.store.lane_disabled(self.job_id, self.key)
                return
            if gate is not None:
                # W122: wait my turn for this stage's shared slot(s) before touching the
                # DB as "running" — a job N+1 whose enrichment/WhatsApp is merely queued
                # behind job N's must not show as started.
                if not self._acquire(gate):                      # W153: parks instead of waiting
                    self.reason = R_STOPPED
                    return
                gate_acquired = True
            self.store.lane_start(self.job_id, self.key)
            self.reason = self.work() or R_COMPLETED
        except Exception as e:                                    # noqa: BLE001
            # Deliberately broad: an unhandled error in one lane must degrade to "this lane
            # failed, with a reason you can read" and never abort the other two.
            #
            # T338 ("add logic to restart and try again") is deliberately NOT a blanket
            # retry-the-whole-lane-thread here: tried that first (MAX_LANE_RETRIES loop with
            # a 15s backoff around this whole block) and it broke two existing invariants —
            # `test_crashed_feeder_releases_the_lane_waiting_on_it` (WhatsApp must stop
            # waiting on a dead enrichment feeder in well under 10s, not 30s+) and
            # `test_enrichment_bails_instead_of_looping_on_a_stuck_queue` (a deterministic,
            # instantly-failing lane must not be re-run 3x for nothing — "a hot infinite
            # loop is a much worse failure than giving up", this file's own module
            # docstring). Retrying belongs at the grain that can actually recover: one
            # number, one browser relaunch, one site — not the whole lane thread. That's
            # what's built: `wa_verify.py`'s WhatsApp lane now retries a single number's
            # navigation instead of raising and killing the lane, and browser crashes on any
            # lane already relaunch via `browser_recovery.Relauncher`. Enrichment's own
            # "stuck queue" bail-out (3 fruitless passes, above) is that same grain applied
            # to its own retry loop.
            log.exception("lane %s failed for job %s", self.key, self.job_id)
            self.reason = f"error:{str(e)[:160]}"
            try:
                self.store.log(self.job_id, self.key, f"lane failed: {e}", "error")
            except Exception:                                     # noqa: BLE001
                pass
        finally:
            try:
                if self.store and self.reason != R_DISABLED:
                    self.store.lane_end(self.job_id, self.key, self.reason or R_COMPLETED)
            finally:
                if gate is not None and gate_acquired:
                    gate.release(self.job_id)
                # Set BEFORE closing the store: a downstream lane blocks on this event, and
                # a lane that crashed must still release the one waiting on it.
                self.done.set()
                if self.store:
                    self.store.close()

    def enabled(self) -> bool:
        return True

    def work(self) -> str | None:
        raise NotImplementedError


class DiscoveryLane(Lane):
    """Google Maps. Unchanged scraping logic — it just no longer runs the phases after it."""

    key = "discovery"

    def enabled(self) -> bool:
        # W62: `discovery_pending` overrides `reenrich_only`. "Finish everything pending"
        # asks for all three lanes at once — retry the failed crawls, check the unverified
        # numbers, and re-open the places saved without their details — and reenrich_only
        # is what normally switches this lane off. Without this the stub re-open was
        # silently dropped from that plan.
        if self.job.get("wa_verify_only"):
            return False
        return not self.job.get("reenrich_only") or bool(self.job.get("discovery_pending"))

    def work(self) -> str | None:
        return self.ctl.run_discovery(self)


class EnrichmentLane(Lane):
    """Website + socials (httpx), then the AI summary for that same lead.

    AI research is folded in here rather than given a lane of its own so that a lead is
    handed to WhatsApp only once everything we know about it is known — the summary needs
    the website enrichment just found anyway.
    """

    key = "enrichment"

    def enabled(self) -> bool:
        return bool(self.job.get("do_enrich", 1)) and not self.job.get("wa_verify_only")

    def work(self) -> str | None:
        from webscraper.enrich import enrich_places

        store = self.store
        assert store is not None
        # Start from what is ALREADY enriched in scope, not 0: a lane resumed after an
        # agent restart keeps the interrupted run's work in the numerator (job #14 showed
        # "1 / ≥ 1" beside 45 emails). Fresh job → 0; scoped re-enrich → its subset was
        # reset to pending, so also 0. Same denominator rule as before.
        seen = store.count_enriched(self.job_id)
        stuck = 0

        # W81 (CRM T500): the re-run's "skip — mark done" choice is answered BEFORE the
        # WhatsApp wait below. It used to sit in that loop for the whole WhatsApp run,
        # reporting "running 575 / 575" on a lane that had nothing to do (job #1631).
        # W76: the re-run's choice about websites (see enrich_scope on the job).
        scope_mode = str(self.job.get("enrich_scope") or "all")
        if scope_mode == "skip":
            # "Mark it done without web scraping": every lead this run would have crawled
            # is settled instead — two attempts is the cutoff the CRM stops counting at —
            # so the job can finish without a crawl that was never wanted.
            keys = store.job_place_keys(self.job_id)
            n = 0
            for r in store.places(self.job_id):
                if keys and r["place_key"] not in keys:
                    continue
                if r["enrich_status"] in ("pending", "failed", "thin"):
                    store.update_enrichment(self.job_id, r["place_key"],
                                            {"enrich_status": "failed" if r["enrich_status"] != "thin" else "thin",
                                             "enrich_attempts": 2,
                                             "enrich_error": r["enrich_error"] or "skipped by request"})
                    n += 1
            self.note(f"websites skipped by request — {n} lead(s) marked settled without a crawl")
            return R_NO_TARGETS

        # W76 (user directive 2026-09-08): websites are the LAST priority. After Maps
        # discovery, WhatsApp verification of the Maps numbers comes first; the site
        # crawl waits until discovery has ended and WhatsApp has nothing left from it.
        # A WhatsApp lane that is off, logged out, or already finished releases the wait
        # at once — otherwise a machine with no session would never crawl anything.
        # WhatsApp keeps running afterwards on the numbers the crawl turns up.
        waited = False
        wa_skip_noted = False
        alongside = False
        wa = self.ctl.whatsapp
        while not self.stopped():
            wa = self.ctl.whatsapp
            wa_running = wa.enabled() and not wa.done.is_set()
            # T928 (2026-09-24): a machine with no WhatsApp account linked (ASUS, DELL, MI)
            # must not have websites held hostage by this wait — the WhatsApp lane just
            # parks in `_wait_for_relink` for as long as this job runs (that wait is what
            # lets a human link one mid-run), which used to mean "websites wait — WhatsApp
            # is checking the Google Maps numbers first" logged once and then nothing moved
            # for the rest of the job. Treat WA verification as off for THIS run instead.
            no_wa_accounts = wa_running and not store.enabled_wa_accounts()
            if no_wa_accounts:
                wa_busy = False
                if not wa_skip_noted:
                    self.note("no WhatsApp account linked on this device — crawling websites "
                              "without waiting for WhatsApp verification")
                    wa_skip_noted = True
            else:
                wa_busy = wa_running and store.count_wa_pending(self.job_id) > 0
            if self.ctl.discovery_finished() and not wa_busy:
                break
            # W138 (CRM T1016, owner: "different lanes of different jobs together to maximise
            # productivity"): the W76 order is a PRIORITY, not a block. An enrichment slot that
            # nobody else is queued for would sit idle for the whole WhatsApp pass (MAC: hours
            # at 60 s/number) — crawl alongside Maps / WhatsApp instead. When another job IS
            # waiting for the slot, keep W76: hand the slot over (W136) and crawl later.
            gate = STAGE_GATES.get(self.key)
            if gate is None or not gate.others_waiting(self.job_id):
                alongside = True
                break
            if not waited and not wa_skip_noted:
                self.note("websites wait — WhatsApp is checking the Google Maps numbers first "
                          "(another job is using the websites slot meanwhile)")
                waited = True
            self._slot_idle("websites are waiting for WhatsApp")      # W136
            time.sleep(IDLE_POLL_SEC)
        if self.stopped():
            return R_STOPPED
        if alongside and (not self.ctl.discovery_finished() or
                          (wa.enabled() and not wa.done.is_set() and store.count_wa_pending(self.job_id) > 0)):
            self.note("crawling websites alongside Maps / WhatsApp — the websites slot was free (W138)")
        elif waited:
            self.note("WhatsApp has cleared the Maps numbers — crawling websites now")

        # Set the total BEFORE the first lead is touched, not just after the first batch
        # finishes. Otherwise `enrich_total` keeps the previous run's value (e.g. 180) and
        # the bar reads "3 / 180" while the 9 fixable leads process, flipping to "9 / 9"
        # only at the very end — the "starting from 0 / 180" the user reported.
        store.update_job(self.job_id, enrich_done=seen,
                         enrich_total=seen + store.count_pending_enrichment(self.job_id))
        while True:
            if self.stopped():
                return R_STOPPED
            batch = store.pending_enrichment(self.job_id, ENRICH_BATCH)
            if batch and scope_mode == "wa_missing":
                # "Only those whose WhatsApp is not found or verified": a lead that already
                # has a verified WhatsApp needs no website; settle it and crawl the rest.
                keep = []
                for r in batch:
                    if str(r.get("wa_verified") or "") == "yes":
                        store.update_enrichment(self.job_id, r["place_key"],
                                                {"enrich_status": "done", "enrich_attempts": 2,
                                                 "enrich_error": None})
                    else:
                        keep.append(r)
                if len(keep) < len(batch):
                    self.note(f"{len(batch) - len(keep)} lead(s) already on WhatsApp — websites skipped for them")
                batch = keep
                if not batch:
                    continue
            if not batch:
                # Nothing waiting. If discovery has finished, nothing ever will be.
                if self.ctl.discovery_finished():
                    if seen and self.research_error:
                        return f"error:AI research: {self.research_error}"[:200]
                    return R_COMPLETED if seen else R_NO_TARGETS
                self._slot_idle("nothing to crawl yet")                 # W136
                time.sleep(IDLE_POLL_SEC)
                continue
            if not self._slot_resume():                                 # W136
                return R_STOPPED

            # The queue is the `places` table, so a row that comes back 'pending' after
            # being processed would be handed to us again forever. enrich_places writes an
            # outcome on every path, so this should not happen — but a hot infinite loop is
            # a much worse failure than giving up, so bail after a few fruitless passes
            # instead of pinning a core and never finishing the job.
            before = {r["place_key"] for r in batch}

            t0 = time.monotonic()
            done = {"n": 0}

            def on_progress(r: dict[str, Any], status: str, fields: dict[str, Any] | None = None) -> None:
                # One log line per lead (T179): what was crawled, how it ended, which tier
                # read it, what it yielded — so the CRM Logs dialog tells the whole story.
                try:
                    self.store.log(self.job_id, "enrichment", _enrich_line(r, status, fields or {}),
                                   "warn" if status == "failed" else "info")
                except Exception:                                 # noqa: BLE001
                    pass
                # A lead with no website is skipped, not enriched: it is outside the
                # denominator (count_pending_enrichment ignores it too), so it must not
                # move the numerator either — keeps done + outstanding = enrichable (T163).
                if status == "no_website":
                    return
                done["n"] += 1
                store.update_job(self.job_id, enrich_done=seen + done["n"])

            # Pass the job's window choice through: a re-enrich run headed ("Show window"
            # in the CRM) must actually open a visible browser on a blocked site. `headless`
            # is stored 1/0; None leaves the module default when the column is unset.
            # Re-read it from the store per batch (T382): a "Show window" re-run that lands
            # while this worker is busy only updates the stored row — the in-memory
            # self.job kept the value the run started with, so the Mac ran job #8 hidden
            # for its whole 350-site pass after the user had asked for a window.
            row = store.get_job(self.job_id)
            fresh = dict(row) if row is not None else {}
            # W65 (user directive 2026-09-08): websites run WINDOWLESS on the first pass and
            # with the window shown on every retry. A first pass is dozens of sites nobody
            # watches; a retry is the handful that failed, and those are exactly the ones
            # where seeing the block happen is the point. `reenrich_only` is what a retry
            # is — the CRM sets it on every "re-run this lane" and every pending run.
            # An explicit "Show window" on the job still wins: stored `headless=0` means
            # the operator asked to watch, and nothing here should argue.
            retry = bool(fresh.get("reenrich_only", self.job.get("reenrich_only")))
            hl = fresh.get("headless", self.job.get("headless"))
            if retry and hl:
                self.store.log(self.job_id, "enrichment",
                               "retry pass — opening a visible browser so blocks are visible")
                hl = False
            if hl is not None and self.job.get("headless") is not None and bool(hl) != bool(self.job.get("headless")):
                self.store.log(self.job_id, "enrichment",
                               "window setting changed mid-run -> " + ("hidden" if hl else "visible") + " from this batch on")
                self.job["headless"] = hl
            self.store.log(self.job_id, "enrichment",
                           f"crawling {len(batch)} website(s): " + ", ".join(
                               (r.get("name") or r.get("place_key") or "?")[:40] for r in batch[:10])
                           + (" …" if len(batch) > 10 else ""))
            store.update_job(self.job_id, enrich_active=len(batch))
            try:
                asyncio.run(enrich_places(store, batch, None, self.job.get("country"),
                                          on_progress, self.stopped,
                                          headless=None if hl is None else bool(hl)))
            finally:
                store.update_job(self.job_id, enrich_active=0)
            still_pending = {r["place_key"] for r in
                             store.pending_enrichment(self.job_id, ENRICH_BATCH)}
            if before & still_pending:
                stuck += 1
                if stuck >= 3:
                    self.note(f"{len(before & still_pending)} leads keep coming back "
                              f"unenriched — stopping this lane rather than looping", "error")
                    return "error:enrichment made no progress on its queue"
            else:
                stuck = 0

            seen += done["n"]
            # Total = what THIS run will process, not every place in the job. `seen` is this
            # run's completed count and `count_pending_enrichment` is what is still queued
            # (scoped to place_keys), so their sum tracks correctly for BOTH a fresh job
            # (pending grows as discovery feeds it) and a re-enrich (a fixed subset). Using
            # count_places here showed "5 / 180" for a 21-lead re-enrich — the "starting
            # from 0" the user reported, because 137 already-done leads inflated the total.
            store.update_job(self.job_id, enrich_done=seen,
                             enrich_total=seen + store.count_pending_enrichment(self.job_id))
            store.record_phase_rate(self.job_id, "enriching", done["n"], time.monotonic() - t0)
            self.note(f"enriched {seen} businesses so far")

            if self.job.get("do_research"):
                self._research(batch)
            if not self._slice_done(len(batch)):            # W135: round-robin across jobs
                return R_STOPPED

    def _research(self, batch: list[dict[str, Any]]) -> None:
        """AI summary for the leads in this batch that ended up with a website."""
        from webscraper.research import research_places

        store = self.store
        assert store is not None
        keys = {r["place_key"] for r in batch}
        targets = [p for p in store.places(self.job_id)
                   if p["place_key"] in keys and p.get("website")]
        if not targets:
            return
        t0 = time.monotonic()
        rdone = {"n": 0}
        base = int(store.get_job(self.job_id)["research_done"] or 0)

        def on_research(_r: dict[str, Any], _s: str) -> None:
            rdone["n"] += 1
            store.update_job(self.job_id, research_done=base + rdone["n"])

        rc = asyncio.run(research_places(store, targets, 3, on_research, self.stopped))
        store.update_job(self.job_id, research_done=base + rdone["n"],
                         research_total=base + rdone["n"])
        store.record_phase_rate(self.job_id, "researching", rdone["n"], time.monotonic() - t0)
        if isinstance(rc, dict) and rc.get("skipped") == len(targets) and targets:
            self.note("AI research skipped — no Gemini key configured", "warn")
        elif isinstance(rc, dict) and rc.get("failed"):
            # Say WHY, not just how many. Job #17 (2026-08-26) reported "50 done" while every
            # Gemini call 429'd; the CRM showed no summary/owner and no reason.
            why = rc.get("error") or "could not read the site text"
            self.note(f"AI research failed for {rc['failed']} of {len(targets)} in this batch — {why}", "warn")
        self._research_targets += len(targets)
        self._research_ai_failed += int((rc.get("gemini_failed") or 0) if isinstance(rc, dict) else 0)
        # W152: at least RESEARCH_ERROR_MIN_FAILED all-provider failures AND half of everything
        # researched so far — a quota outage (job #17) still ends the lane with the reason; a few
        # transient network misses on a free tier no longer fail a lane that enriched every site.
        if (self._research_ai_failed >= RESEARCH_ERROR_MIN_FAILED
                and self._research_ai_failed * 2 >= self._research_targets):
            self.research_error = ((rc.get("error") if isinstance(rc, dict) else None)
                                   or self.research_error or "every AI provider failed")
        else:
            self.research_error = None


class WhatsAppLane(Lane):
    """Verify numbers on WhatsApp Web, one lead at a time, as enrichment releases them.

    Kept deliberately slow (randomised pacing, per-account daily cap) — this drives a real
    account and the ban risk is never zero. What changes is only that it now starts at
    minute 0 instead of inheriting whatever time the other phases left it.
    """

    key = "whatsapp"

    def enabled(self) -> bool:
        return bool(self.job.get("do_wa_verify")) or bool(self.job.get("wa_verify_only"))

    def work(self) -> str | None:
        try:
            return self._work()
        finally:
            # Whatever way the loop exits, nothing is in flight any more.
            if self.store is not None:
                try:
                    self.store.update_job(self.job_id, wa_active=0)
                except Exception:                                 # noqa: BLE001
                    pass

    def _work(self) -> str | None:
        from webscraper import wa_verify

        store = self.store
        assert store is not None
        checked = 0
        decided = 0                                                 # W143: yes/no only
        t0 = time.monotonic()
        while True:
            if self.stopped():
                return R_STOPPED
            batch = store.pending_wa_verify(self.job_id, 25)
            if not batch:
                if self.ctl.enrichment_finished():
                    return R_COMPLETED if checked else R_NO_TARGETS
                self._slot_idle("no numbers to check yet")             # W136
                time.sleep(IDLE_POLL_SEC)
                continue
            if not self._slot_resume():                                 # W136
                return R_STOPPED

            # Units are NUMBERS (W26): a business with a Maps phone, a wa.me link and two
            # numbers on its site is four checks, and the bar counts all four.
            store.update_job(self.job_id,
                             wa_verify_total=checked + store.count_wa_pending(self.job_id),
                             wa_verify_done=checked, wa_active=len(batch))

            by_pk = {r["place_key"]: r for r in batch}
            # W102: the descriptor count at every batch boundary. Each batch starts one
            # Playwright driver per slice and ends it; a number that climbs batch after
            # batch is a leak, and on macOS (256 by default) it is the `[Errno 24]` that
            # ended job #6619's lane after ~2 h. Logged here so the trend is visible in
            # agent.log and the job log long before the cap.
            from webscraper.fdcount import fd_status
            fds = fd_status()
            log.info("[whatsapp#%s] batch of %d — %s", self.job_id, len(batch), fds)
            self.store.log(self.job_id, "whatsapp",
                           f"checking {len(batch)} number(s) on WhatsApp — accounts: "
                           f"{', '.join(store.enabled_wa_accounts()) or 'none'}"
                           + (" · no daily cap" if settings.wa_daily_cap <= 0 else f" · cap {settings.wa_daily_cap}/day")
                           + f" · {fds}")

            # W78: the progress callback takes the Store of the THREAD that calls it. The
            # single-session path passes the lane's own; each W76 slice passes the Store
            # it opened for itself. sqlite3 connections are bound to the thread that
            # created them — handing a slice the lane's `on_wa`/`stopped` raised
            # `ProgrammingError: SQLite objects created in a thread can only be used in
            # that same thread` on the first `should_stop()`, so 2-session mode died
            # before it had checked one number (1 - PC, job 36, 2026-09-09).
            counter_lock = threading.Lock()

            def make_on_wa(st: Store):
                def on_wa(pk: str, status: str, num: str | None = None, source: str | None = None) -> None:
                    nonlocal checked, decided
                    with counter_lock:
                        checked += 1
                        done_now = checked
                        if status in ("yes", "no"):
                            decided += 1                            # W143: the honest "useful output"
                        dec_now = decided
                    st.update_job(self.job_id, wa_verify_done=done_now, wa_decided=dec_now)
                    try:
                        r = by_pk.get(pk, {})
                        st.log(self.job_id, "whatsapp",
                               _wa_line(r, status, num or r.get("number"), source or r.get("source")))
                    except Exception:                             # noqa: BLE001
                        pass
                return on_wa

            on_wa = make_on_wa(store)

            try:
                # W59: this lane's own window choice. NULL on the job (the normal
                # case, and every job made before this) means "whatever the agent's
                # WA_VERIFY_HEADLESS says", which is exactly the old behaviour.
                wa_hl = self.job.get("wa_headless")
                hl = None if wa_hl is None else bool(wa_hl)
                # W76: parallel sessions. One WhatsApp Web session per LINKED account —
                # a number cannot be open twice — so the ceiling is the smaller of the
                # machine's setting and the accounts that are actually linked. Each slice
                # pins its own account and its own sqlite connection; the progress
                # callback is the one shared thing and is locked.
                accounts = store.enabled_wa_accounts()
                want = _wa_parallel()
                n = max(1, min(want, len(accounts)))
                if n > 1:
                    self.store.log(self.job_id, "whatsapp",
                                   f"{n} WhatsApp sessions in parallel — {', '.join(accounts[:n])}")
                    slices = [batch[i::n] for i in range(n)]
                    results: list[dict] = []
                    errors: list[BaseException] = []
                    def run_slice(rows, name):
                        # Everything this thread touches in sqlite goes through `st`:
                        # the verify itself, its progress lines, and the stop poll.
                        st = self.ctl.new_store()
                        try:
                            results.append(wa_verify.verify_places(
                                st, rows, make_on_wa(st),
                                lambda: bool(st.stop_requested(self.job_id)),
                                job_id=self.job_id, headless=hl, account=name))
                        except BaseException as e:                # noqa: BLE001
                            errors.append(e)
                            log.warning("[whatsapp#%s] slice %s failed: %s", self.job_id, name, e)
                        finally:
                            st.close()
                    ts = [threading.Thread(target=run_slice, args=(sl, accounts[i]),
                                           name=f"wa-{i}", daemon=True)
                          for i, sl in enumerate(slices) if sl]
                    for t in ts:
                        t.start()
                    for t in ts:
                        t.join()
                    if errors and not results:
                        raise errors[0]
                    # W78: the summary line below reads yes/no/unknown — a `capped`-only
                    # dict was a KeyError waiting behind the thread bug.
                    res = {k: sum(int(r.get(k, 0) or 0) for r in results)
                           for k in ("yes", "no", "unknown", "no_number")}
                    res["capped"] = any(bool(r.get("capped")) for r in results)
                    # W143: the W137/W143 signals were dropped by this merge in parallel mode.
                    res["sync_blocked"] = (any(bool(r.get("sync_blocked")) for r in results)
                                           and res["yes"] + res["no"] == 0)
                    res["needs_relink"] = sum(int(r.get("needs_relink", 0) or 0) for r in results)
                else:
                    res = wa_verify.verify_places(store, batch, on_wa, self.stopped,
                                                  job_id=self.job_id, headless=hl)
            except wa_verify.WaNotLoggedIn as e:
                # W77: a login window is open on this machine (the user is linking a
                # number from the CRM's "WhatsApp login" mid-job). The rotation reached
                # that account and _ensure_session refused to open its profile — which is
                # right — but giving the lane up for it is not: the job's WhatsApp lane
                # ended for good and the new number never got used. Wait the login out
                # (its QR window times out after 2 min) and carry on with the same batch.
                if wa_verify.login_in_progress():
                    self.note("WhatsApp lane paused — a WhatsApp login is open on this "
                              "machine; resuming when it closes", "info")
                    self._slot_idle("a WhatsApp login is open", force=True)   # W136
                    deadline = time.monotonic() + WA_LOGIN_WAIT_SEC
                    while wa_verify.login_in_progress() and time.monotonic() < deadline:
                        if self.stopped():
                            return R_STOPPED
                        time.sleep(1.0)
                    if not wa_verify.login_in_progress():
                        continue
                # W110 (CRM T601): "no linked session right now" is not "never". Job #103 on
                # 1 - PC ended this lane at 11:26 IST because hvt_wa_bus_2 had dropped; the
                # user re-linked it at 14:24 while discovery was still adding numbers, and
                # 1,189 of them sat unchecked because nothing ever looked again.
                self._slot_idle("no WhatsApp session right now", force=True)  # W136
                waited = _wait_for_relink(self, store, e)
                if waited == R_STOPPED:
                    return R_STOPPED
                if waited == R_WA_NO_SESSION:
                    # W144 (CRM T1024): same end shape as a `wa_daily_cap` stop — the WhatsApp
                    # total/done counters carry the leftover (numbers still pending), the job ends
                    # with this lane's reason and the CRM moves its WhatsApp pass elsewhere.
                    mins = int(wa_no_session_give_up_sec() // 60)
                    self.note(f"WhatsApp lane gave up after {mins} min without a linked account — the CRM "
                              "moves this job's WhatsApp pass to a machine with a session", "warn")
                    done_n = store.count_wa_done(self.job_id)
                    store.update_job(self.job_id, wa_verify_done=done_n,
                                     wa_verify_total=done_n + store.count_wa_pending(self.job_id))
                    return R_WA_NO_SESSION
                if waited:
                    continue
                self.note(f"WhatsApp verification skipped — {e}", "warn")
                # W67: and take the window with it. The lane gives up in seconds, but the
                # headed WhatsApp browser it opened was left sitting on the splash — the
                # Mac had one on screen for an afternoon, which reads as "the job is
                # stuck" when the job was running perfectly well without it.
                try:
                    from webscraper.browser_recovery import reap_orphan_browsers
                    from webscraper.config import settings as _st
                    # W68: never while a login is open — that window is the one the user is
                    # scanning, and killing it is how "Start WhatsApp session" started
                    # failing with "target page, context or browser has been closed".
                    killed = 0 if wa_verify.login_in_progress() else reap_orphan_browsers(
                        [d for d in _st.wa_profiles_dir.iterdir()
                         if d.is_dir() and not wa_verify.login_in_progress(d.name)],
                        "whatsapp lane gave up")
                    if killed:
                        self.note(f"closed {killed} leftover WhatsApp window(s)", "info")
                except Exception:                                 # noqa: BLE001
                    pass
                return R_WA_LOGIN
            if res.get("needs_relink"):
                # W143: the ladder flagged the account(s) — no park. The next batch finds no enabled
                # account and takes the W110 relink wait; the CRM shows NEEDS RELINK in the self-check.
                self._sync_parks = 0
                self.note(f"{res['needs_relink']} WhatsApp account(s) flagged NEEDS RELINK — WhatsApp Web never "
                          "finished syncing; open Lead Finder > Systems > WhatsApp login and scan the QR", "error")
                continue
            if res.get("sync_blocked"):
                # W137: nothing decided — WhatsApp Web on this machine never left its sync
                # splash; the numbers went back on the queue untouched. W143: the account's own
                # ladder escalates (recovery, then needs_relink); the give-up below is a backstop.
                parks = getattr(self, "_sync_parks", 0) + 1
                self._sync_parks = parks
                if parks >= WA_SYNC_GIVE_UP:
                    self.note(f"WhatsApp Web on this machine kept re-syncing for "
                              f"{int(WA_SYNC_PARK_SEC * WA_SYNC_GIVE_UP / 60)} min — WhatsApp verification "
                              "stopped for this run; open WhatsApp on the phone / relink the account "
                              "in Lead Finder > Systems", "error")
                    return "error:WhatsApp Web keeps re-syncing — relink on this machine"
                self.note(f"WhatsApp Web keeps re-syncing — pausing WhatsApp checks "
                          f"{int(WA_SYNC_PARK_SEC / 60)} min (pause {parks}); the numbers stay queued — "
                          "the account escalates to a browser recovery, then to NEEDS RELINK (W143)", "warn")
                self._slot_idle("WhatsApp Web is re-syncing", force=True)
                deadline = time.monotonic() + WA_SYNC_PARK_SEC
                while time.monotonic() < deadline:
                    if self.stopped():
                        return R_STOPPED
                    time.sleep(1.0)
                continue
            self._sync_parks = 0
            store.record_phase_rate(self.job_id, "verifying_wa", checked,
                                    time.monotonic() - t0)
            store.update_job(self.job_id, wa_verify_done=checked,
                             wa_verify_total=checked + store.count_wa_pending(self.job_id))
            self.note(f"WhatsApp: {res['yes']} on WA, {res['no']} not, "
                      f"{res['unknown']} unknown ({checked} numbers checked)")
            if res.get("capped"):
                return R_WA_CAP
            if not self._slice_done(len(batch)):            # W135: round-robin across jobs
                return R_STOPPED


def _wa_parallel() -> int:
    """W76: how many WhatsApp sessions this machine may run at once (default 1).

    Set per machine on the CRM Systems tab as the lead_gen_settings key
    `wa_parallel__<device>`, pushed to the agent with the rest of the config at start
    (so it takes effect on the next restart), with `WA_PARALLEL` in .env as a local
    override. Capped at 4: each session is a full Chrome, and a laptop that is also
    crawling websites has nothing to spare past that.
    """
    import os
    try:
        from webscraper.agent import DEVICE_NAME
    except Exception:                                             # noqa: BLE001
        DEVICE_NAME = ""
    raw = (os.getenv(f"WA_PARALLEL__{DEVICE_NAME.upper()}") if DEVICE_NAME else None) \
        or os.getenv("WA_PARALLEL") or "1"
    try:
        return max(1, min(4, int(str(raw).strip())))
    except ValueError:
        return 1


#: W135: local job id -> its running Pipeline, for the agent's liveness ping / watchdog.
_PIPELINES: dict[int, "Pipeline"] = {}


def alive_lanes(job_id: int) -> list[str]:
    """W135: keys of the lane threads still alive for `job_id` ([] when the Worker has
    not started it, or it has finished)."""
    p = _PIPELINES.get(job_id)
    if p is None:
        return []
    return [l.key for l in p.lanes if l.is_alive()]


def lane_states(job_id: int) -> dict[str, str]:
    """W135: per live lane: `running` (holds its stage slot / no gate), `queued` (waiting
    for a slot — including a W135 yield), `idle` (W136: gave its slot up while it has
    nothing to do) — read by `agent._cloud_phase`."""
    out: dict[str, str] = {}
    p = _PIPELINES.get(job_id)
    lanes_by_key = {l.key: l for l in p.lanes} if p is not None else {}
    for key in alive_lanes(job_id):
        g = STAGE_GATES.get(key)
        if g is not None and g.waiting_on(job_id):
            out[key] = "queued"
        elif g is not None and getattr(lanes_by_key.get(key), "_slot_parked", False):
            out[key] = "idle"
        else:
            out[key] = "running"
    return out


class Pipeline:
    """Runs the three lanes for one job and reports how each of them ended."""

    def __init__(self, job_id: int, job: dict[str, Any],
                 discovery_fn: Callable[[Lane], str | None],
                 store_factory: Callable[[], Store] = Store) -> None:
        self.job_id = job_id
        self.job = job
        self._store_factory = store_factory
        # Discovery's body stays in server.py: it needs the worker's areas/budget/on_event
        # closures, and moving it here would drag half the worker along with it.
        self._discovery_fn = discovery_fn
        self.discovery = DiscoveryLane(job_id, job, self)
        self.enrichment = EnrichmentLane(job_id, job, self)
        self.whatsapp = WhatsAppLane(job_id, job, self)
        self.lanes = [self.discovery, self.enrichment, self.whatsapp]

    def new_store(self) -> Store:
        """One connection per lane — sqlite3 connections are not thread-safe."""
        return self._store_factory()

    # Callbacks the downstream lanes use to know when their input is exhausted.
    def run_discovery(self, lane: Lane) -> str | None:
        return self._discovery_fn(lane)

    def discovery_finished(self) -> bool:
        return self.discovery.done.is_set()

    def discovery_slot_free(self) -> bool:
        """W122: true once this job no longer needs the ONE shared Maps tab — either it
        never asked for discovery, or discovery has ended — so the Worker can let the
        next job's discovery start."""
        return not self.discovery.enabled() or self.discovery.done.is_set()

    def other_lanes_done(self, lane: "Lane") -> bool:
        """W153: is `lane` the only lane of this job still alive? (the others disabled or ended)"""
        return all(l is lane or not l.enabled() or l.done.is_set() for l in self.lanes)

    def enrichment_finished(self) -> bool:
        # A job with enrichment switched off still feeds WhatsApp: discovery writes the
        # Maps phone straight onto the row, so WhatsApp's input dries up when DISCOVERY
        # ends. Without this the WhatsApp lane would spin until the job was stopped.
        if not self.enrichment.enabled():
            return self.discovery_finished()
        return self.enrichment.done.is_set()

    def run(self) -> dict[str, str | None]:
        _PIPELINES[self.job_id] = self                       # W135: liveness registry
        try:
            for lane in self.lanes:
                lane.start()
            for lane in self.lanes:
                lane.join()
            return {lane.key: lane.reason for lane in self.lanes}
        finally:
            _PIPELINES.pop(self.job_id, None)

    def summary(self) -> str:
        """One line for jobs.message once everything has stopped."""
        bits = [f"{lane.key}: {lane.reason}" for lane in self.lanes
                if lane.reason and lane.reason != R_DISABLED]
        return " · ".join(bits) or "nothing to do"


#: One gate per stage, shared by every job's lanes for the life of the process. Built at
#: the END of the module (W135: `wa_slots()` needs `_wa_parallel`, defined above). Discovery
#: has no entry here — it is serialised by the Worker (one job's Maps tab at a time), not
#: by this gate.
STAGE_GATES: dict[str, StageGate] = _build_stage_gates()


def reset_stage_gates() -> None:
    """Test hook: rebuild the module-level stage gates — fresh queues, and re-reads the
    env vars (so a test's `monkeypatch.setenv` before calling this takes effect)."""
    STAGE_GATES.clear()
    STAGE_GATES.update(_build_stage_gates())
