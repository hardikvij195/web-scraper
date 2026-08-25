"""Three concurrent lanes for one job: discovery → enrichment → WhatsApp.

Before this (2026-08-23) the worker ran four phases strictly in sequence against two
budgets, and WhatsApp — last in line and the slowest by design — always lost the race. Live
job #6 is the proof: `WhatsApp verification 2 / 30 numbers` labelled *done*, because
enrichment and AI research had spent the shared post-Maps budget and the lane exited after
two checks. Running the lanes at the same time removes the race instead of re-tuning it.

    Lane A  discovery    Maps Chrome, single-threaded (the proxy-pool rule stands)
       │  writes places rows with enrich_status='pending'
       ▼
    Lane B  enrichment   httpx site+socials, then the AI summary for that same lead
       │  writes enrich_status / research_status
       ▼
    Lane C  whatsapp     WhatsApp Web Chrome, existing pacing + per-account daily cap

**The `places` table is the queue.** Lanes hand each other nothing in memory: B selects
`enrich_status='pending'`, C selects rows whose enrichment has resolved and whose
`wa_verified` is still undecided. That was chosen over a `queue.Queue` for two reasons —
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
        seen = 0
        stuck = 0
        # Set the total BEFORE the first lead is touched, not just after the first batch
        # finishes. Otherwise `enrich_total` keeps the previous run's value (e.g. 180) and
        # the bar reads "3 / 180" while the 9 fixable leads process, flipping to "9 / 9"
        # only at the very end — the "starting from 0 / 180" the user reported. At the
        # start `seen` is 0, so this is simply the count of work queued for this run.
        store.update_job(self.job_id, enrich_done=0,
                         enrich_total=store.count_pending_enrichment(self.job_id))
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

            def on_progress(_r: dict[str, Any], status: str) -> None:
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
            asyncio.run(enrich_places(store, batch, None, self.job.get("country"),
                                      on_progress, self.stopped,
                                      headless=None if hl is None else bool(hl)))
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

            store.update_job(self.job_id,
                             wa_verify_total=checked + len(batch),
                             wa_verify_done=checked)

            def on_wa(_pk: str, _status: str, _num: str | None = None) -> None:
                nonlocal checked
                checked += 1
                store.update_job(self.job_id, wa_verify_done=checked)

            try:
                res = wa_verify.verify_places(store, batch, on_wa, self.stopped,
                                              job_id=self.job_id)
            except wa_verify.WaNotLoggedIn as e:
                self.note(f"WhatsApp verification skipped — {e}", "warn")
                return R_WA_LOGIN
            store.record_phase_rate(self.job_id, "verifying_wa", checked,
                                    time.monotonic() - t0)
            self.note(f"WhatsApp: {res['yes']} on WA, {res['no']} not, "
                      f"{res['unknown']} unknown ({checked} checked)")
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
