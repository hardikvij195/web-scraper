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



def _enrich_line(r: dict, status: str, f: dict) -> str:
    """'Name · site → done via tls · 1 email, instagram, whatsapp' / '… → FAILED: http_403'."""
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
        return f"{name} · {site} → FAILED: {f.get('enrich_error') or 'unknown'}"
    tail = ", ".join(found) if found else "no contacts found"
    return f"{name} · {site} → {status}{via} · {tail}"


#: Where a checked number came from, as the log line says it (W26).
WA_SOURCE_LABEL = {"maps": "maps", "wa_link": "whatsapp link", "site": "website"}


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
        return not self.job.get("wa_verify_only") and not self.job.get("reenrich_only")

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
            if not batch:
                # Nothing waiting. If discovery has finished, nothing ever will be.
                if self.ctl.discovery_finished():
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
            hl = self.job.get("headless")
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

            def on_wa(pk: str, status: str, num: str | None = None, source: str | None = None) -> None:
                nonlocal checked
                checked += 1
                store.update_job(self.job_id, wa_verify_done=checked)
                try:
                    r = by_pk.get(pk, {})
                    self.store.log(self.job_id, "whatsapp",
                                   _wa_line(r, status, num or r.get("number"), source or r.get("source")))
                except Exception:                                 # noqa: BLE001
                    pass

            try:
                res = wa_verify.verify_places(store, batch, on_wa, self.stopped,
                                              job_id=self.job_id)
            except wa_verify.WaNotLoggedIn as e:
                self.note(f"WhatsApp verification skipped — {e}", "warn")
                return R_WA_LOGIN
            store.record_phase_rate(self.job_id, "verifying_wa", checked,
                                    time.monotonic() - t0)
            store.update_job(self.job_id, wa_verify_done=checked,
                             wa_verify_total=checked + store.count_wa_pending(self.job_id))
            self.note(f"WhatsApp: {res['yes']} on WA, {res['no']} not, "
                      f"{res['unknown']} unknown ({checked} numbers checked)")
            if res.get("capped"):
                return R_WA_CAP


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
