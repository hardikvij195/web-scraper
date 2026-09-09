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
import re
import threading
import time
from typing import Any, Callable

from webscraper.config import settings
from webscraper.store import Store

log = logging.getLogger("webscraper.lanes")

#: How long a downstream lane waits before re-checking its queue when it is empty but the
#: lane feeding it is still running. Two seconds is invisible next to a ~3.5 s/place scrape
#: and keeps the polling cost to nothing.
IDLE_POLL_SEC = 2.0
# W77: how long the WhatsApp lane waits for an open wa-login window before giving up —
# the QR window itself times out after 2 min, so this only ever waits out a real scan.
WA_LOGIN_WAIT_SEC = 240.0

#: Enrichment gets its speed from concurrency inside `enrich_places`, so it takes a batch
#: rather than one lead at a time. Small enough that a lead reaches WhatsApp quickly.
ENRICH_BATCH = 10

#: Reason tokens. `ok` is true only for the first two — see Store.OK_REASONS.
R_COMPLETED = "completed"          # ran out of work: the honest "done"
R_NO_TARGETS = "no_targets"        # nothing qualified for this lane
R_MAPS_CAP = "maps_cap"            # discovery hit max_minutes
R_STOPPED = "stopped"              # user pressed Stop
R_WA_CAP = "wa_daily_cap"          # per-account WhatsApp cap reached
R_WA_LOGIN = "wa_not_logged_in"    # no live WhatsApp Web session
R_DISABLED = "disabled"            # the job did not ask for this lane



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

    # -- helpers ---------------------------------------------------------------------
    def note(self, message: str, level: str = "info") -> None:
        """One line into the job's log history AND the latest-message field."""
        if self.store:
            self.store.log(self.job_id, self.key, message, level)
            self.store.update_job(self.job_id, message=message)
        log.info("[%s#%s] %s", self.key, self.job_id, message)

    def stopped(self) -> bool:
        return bool(self.store and self.store.stop_requested(self.job_id))

    # -- thread body -----------------------------------------------------------------
    def run(self) -> None:
        # Built through the pipeline's factory rather than `Store()` directly so a test can
        # point the lanes at a temp DB — and so the "one connection per lane" rule stays
        # visible at the single place it is enforced.
        self.store = self.ctl.new_store()
        try:
            if not self.enabled():
                self.reason = R_DISABLED
                # Record it, and wipe any stamp left by an earlier pass of this job.
                # A re-run reuses the row, so yesterday's `scrape_started_at` would
                # otherwise make a switched-off lane read as still running.
                self.store.lane_disabled(self.job_id, self.key)
                return
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

        # W76 (user directive 2026-09-08): websites are the LAST priority. After Maps
        # discovery, WhatsApp verification of the Maps numbers comes first; the site
        # crawl waits until discovery has ended and WhatsApp has nothing left from it.
        # A WhatsApp lane that is off, logged out, or already finished releases the wait
        # at once — otherwise a machine with no session would never crawl anything.
        # WhatsApp keeps running afterwards on the numbers the crawl turns up.
        waited = False
        while not self.stopped():
            wa = self.ctl.whatsapp
            wa_busy = wa.enabled() and not wa.done.is_set() and store.count_wa_pending(self.job_id) > 0
            if self.ctl.discovery_finished() and not wa_busy:
                break
            if not waited:
                self.note("websites wait — WhatsApp is checking the Google Maps numbers first")
                waited = True
            time.sleep(IDLE_POLL_SEC)
        if self.stopped():
            return R_STOPPED
        if waited:
            self.note("WhatsApp has cleared the Maps numbers — crawling websites now")

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
                time.sleep(IDLE_POLL_SEC)
                continue

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
            if (rc.get("gemini_failed") or 0) >= max(1, len(targets) // 2):
                self.research_error = why


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
        t0 = time.monotonic()
        while True:
            if self.stopped():
                return R_STOPPED
            batch = store.pending_wa_verify(self.job_id, 25)
            if not batch:
                if self.ctl.enrichment_finished():
                    return R_COMPLETED if checked else R_NO_TARGETS
                time.sleep(IDLE_POLL_SEC)
                continue

            # Units are NUMBERS (W26): a business with a Maps phone, a wa.me link and two
            # numbers on its site is four checks, and the bar counts all four.
            store.update_job(self.job_id,
                             wa_verify_total=checked + store.count_wa_pending(self.job_id),
                             wa_verify_done=checked, wa_active=len(batch))

            by_pk = {r["place_key"]: r for r in batch}
            self.store.log(self.job_id, "whatsapp",
                           f"checking {len(batch)} number(s) on WhatsApp — accounts: "
                           f"{', '.join(store.enabled_wa_accounts()) or 'none'}"
                           + (" · no daily cap" if settings.wa_daily_cap <= 0 else f" · cap {settings.wa_daily_cap}/day"))

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
                    nonlocal checked
                    with counter_lock:
                        checked += 1
                        done_now = checked
                    st.update_job(self.job_id, wa_verify_done=done_now)
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
                    deadline = time.monotonic() + WA_LOGIN_WAIT_SEC
                    while wa_verify.login_in_progress() and time.monotonic() < deadline:
                        if self.stopped():
                            return R_STOPPED
                        time.sleep(1.0)
                    if not wa_verify.login_in_progress():
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
            store.record_phase_rate(self.job_id, "verifying_wa", checked,
                                    time.monotonic() - t0)
            store.update_job(self.job_id, wa_verify_done=checked,
                             wa_verify_total=checked + store.count_wa_pending(self.job_id))
            self.note(f"WhatsApp: {res['yes']} on WA, {res['no']} not, "
                      f"{res['unknown']} unknown ({checked} numbers checked)")
            if res.get("capped"):
                return R_WA_CAP


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

    def enrichment_finished(self) -> bool:
        # A job with enrichment switched off still feeds WhatsApp: discovery writes the
        # Maps phone straight onto the row, so WhatsApp's input dries up when DISCOVERY
        # ends. Without this the WhatsApp lane would spin until the job was stopped.
        if not self.enrichment.enabled():
            return self.discovery_finished()
        return self.enrichment.done.is_set()

    def run(self) -> dict[str, str | None]:
        for lane in self.lanes:
            lane.start()
        for lane in self.lanes:
            lane.join()
        return {lane.key: lane.reason for lane in self.lanes}

    def summary(self) -> str:
        """One line for jobs.message once everything has stopped."""
        bits = [f"{lane.key}: {lane.reason}" for lane in self.lanes
                if lane.reason and lane.reason != R_DISABLED]
        return " · ".join(bits) or "nothing to do"
