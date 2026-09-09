"""Lane pipeline: hand-off, termination and failure isolation.

These are the properties the whole 2026-08-23 change rests on, and none of them are visible
from a unit test of any single lane:

  * a lead written by discovery reaches enrichment WHILE discovery is still running,
  * a downstream lane keeps draining after its feeder stops, then exits by itself,
  * a lane that raises records `ok=0` with a reason and does NOT take its siblings down,
  * a crashed feeder still releases the lane waiting on it (otherwise WhatsApp spins forever).

Everything runs against a temp SQLite file, never `data/leads.db`.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from webscraper import lanes as L
from webscraper.store import Store, now_iso


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "test.db"
    return lambda: Store(path)


def _mk_job(new_store, **over) -> tuple[int, dict]:
    s = new_store()
    job_id = s.create_job(query="q", location="here", max_places=10, delay_sec=0)
    job = {"do_enrich": 1, "do_research": 0, "do_wa_verify": 0, "country": "IN"}
    job.update(over)
    s.close()
    return job_id, job


def _add_place(new_store, job_id: int, key: str, phone: str | None = None) -> None:
    s = new_store()
    s.conn.execute(
        "INSERT OR IGNORE INTO places(job_id, place_key, name, phone, enrich_status, scraped_at) "
        "VALUES (?,?,?,?, 'pending', ?)", (job_id, key, f"biz {key}", phone, now_iso()))
    s.conn.commit()
    s.close()


class _FakeEnrichLane(L.EnrichmentLane):
    """Enrichment without httpx: marks whatever it is handed as enriched."""

    def work(self):
        store = self.store
        seen = 0
        while True:
            if self.stopped():
                return L.R_STOPPED
            batch = store.pending_enrichment(self.job_id, L.ENRICH_BATCH)
            if not batch:
                if self.ctl.discovery_finished():
                    return L.R_COMPLETED if seen else L.R_NO_TARGETS
                time.sleep(0.05)
                continue
            for r in batch:
                store.conn.execute(
                    "UPDATE places SET enrich_status='done' WHERE job_id=? AND place_key=?",
                    (self.job_id, r["place_key"]))
                seen += 1
            store.conn.commit()
            store.update_job(self.job_id, enrich_done=seen)


def _pipeline(new_store, job_id, job, discovery_fn, enrich_cls=_FakeEnrichLane):
    pipe = L.Pipeline(job_id, job, discovery_fn, store_factory=new_store)
    pipe.enrichment = enrich_cls(job_id, job, pipe)
    pipe.lanes = [pipe.discovery, pipe.enrichment, pipe.whatsapp]
    return pipe


def test_lead_reaches_enrichment_while_discovery_still_running(db):
    """The whole point: enrichment must not wait for discovery to finish."""
    job_id, job = _mk_job(db)
    seen_while_running = {"n": 0}

    def discovery(lane):
        for i in range(6):
            _add_place(db, job_id, f"p{i}")
            time.sleep(0.08)
            # How many has enrichment already cleared while we are still discovering?
            s = db()
            done = s.conn.execute(
                "SELECT COUNT(*) FROM places WHERE job_id=? AND enrich_status='done'",
                (job_id,)).fetchone()[0]
            s.close()
            seen_while_running["n"] = max(seen_while_running["n"], done)
        return L.R_COMPLETED

    pipe = _pipeline(db, job_id, job, discovery)
    pipe.run()

    assert seen_while_running["n"] > 0, "enrichment never ran concurrently with discovery"
    s = db()
    assert s.conn.execute(
        "SELECT COUNT(*) FROM places WHERE job_id=? AND enrich_status='pending'",
        (job_id,)).fetchone()[0] == 0, "enrichment did not drain the backlog"
    s.close()


def test_enrichment_drains_then_exits_after_discovery_ends(db):
    job_id, job = _mk_job(db)

    def discovery(lane):
        for i in range(4):
            _add_place(db, job_id, f"p{i}")
        return L.R_COMPLETED

    pipe = _pipeline(db, job_id, job, discovery)
    reasons = pipe.run()
    assert reasons["discovery"] == L.R_COMPLETED
    assert reasons["enrichment"] == L.R_COMPLETED


def test_lane_with_no_work_reports_no_targets(db):
    job_id, job = _mk_job(db)
    pipe = _pipeline(db, job_id, job, lambda lane: L.R_COMPLETED)
    reasons = pipe.run()
    assert reasons["enrichment"] == L.R_NO_TARGETS
    s = db()
    assert s.lanes(job_id)["enrichment"]["ok"] is True   # no_targets is a success, not a failure
    s.close()


def test_disabled_lane_is_marked_disabled_and_skipped(db):
    job_id, job = _mk_job(db, do_enrich=0, do_wa_verify=0)
    pipe = _pipeline(db, job_id, job, lambda lane: L.R_COMPLETED)
    reasons = pipe.run()
    assert reasons["enrichment"] == L.R_DISABLED
    assert reasons["whatsapp"] == L.R_DISABLED


def test_a_crashing_lane_does_not_take_the_others_down(db):
    """A dead lane must not cost the work its siblings already did."""
    job_id, job = _mk_job(db)

    class _Boom(_FakeEnrichLane):
        def work(self):
            raise RuntimeError("browser exploded")

    def discovery(lane):
        _add_place(db, job_id, "p0")
        return L.R_COMPLETED

    pipe = _pipeline(db, job_id, job, discovery, enrich_cls=_Boom)
    reasons = pipe.run()

    assert reasons["discovery"] == L.R_COMPLETED, "a sibling lane's crash killed discovery"
    assert reasons["enrichment"].startswith("error:")
    assert "browser exploded" in reasons["enrichment"]
    s = db()
    lane = s.lanes(job_id)["enrichment"]
    assert lane["ok"] is False and lane["ended_at"], "a crashed lane must still record an end"
    s.close()


def test_crashed_feeder_releases_the_lane_waiting_on_it(db):
    """If enrichment dies, WhatsApp must stop waiting rather than spin forever."""
    job_id, job = _mk_job(db, do_wa_verify=1)

    class _Boom(_FakeEnrichLane):
        def work(self):
            raise RuntimeError("nope")

    class _CountingWa(L.WhatsAppLane):
        def work(self):
            while True:
                if self.stopped():
                    return L.R_STOPPED
                if not self.store.pending_wa_verify(self.job_id, 25):
                    if self.ctl.enrichment_finished():
                        return L.R_NO_TARGETS
                    time.sleep(0.05)
                    continue
                return L.R_COMPLETED

    pipe = L.Pipeline(job_id, job, lambda lane: L.R_COMPLETED, store_factory=db)
    pipe.enrichment = _Boom(job_id, job, pipe)
    pipe.whatsapp = _CountingWa(job_id, job, pipe)
    pipe.lanes = [pipe.discovery, pipe.enrichment, pipe.whatsapp]

    start = time.monotonic()
    reasons = pipe.run()
    assert time.monotonic() - start < 10, "WhatsApp lane hung waiting on a dead feeder"
    assert reasons["whatsapp"] == L.R_NO_TARGETS


def test_reasons_map_to_ok_correctly(db):
    """`ok` is what stops a lane that gave up from rendering as done."""
    job_id, _ = _mk_job(db)
    s = db()
    for reason, expected in ((L.R_COMPLETED, True), (L.R_NO_TARGETS, True),
                             (L.R_MAPS_CAP, False), (L.R_WA_CAP, False),
                             (L.R_STOPPED, False), ("error:boom", False)):
        s.lane_end(job_id, "whatsapp", reason)
        assert s.lanes(job_id)["whatsapp"]["ok"] is expected, reason
    s.close()


def test_whatsapp_without_enrichment_follows_discovery(db):
    """Enrichment off still feeds WhatsApp — discovery writes the Maps phone directly, so
    WhatsApp's input dries up when DISCOVERY ends, not when enrichment does."""
    job_id, job = _mk_job(db, do_enrich=0, do_wa_verify=1)
    pipe = L.Pipeline(job_id, job, lambda lane: L.R_COMPLETED, store_factory=db)
    assert pipe.enrichment.enabled() is False
    pipe.discovery.done.set()
    assert pipe.enrichment_finished() is True


def test_enrichment_bails_instead_of_looping_on_a_stuck_queue(db, monkeypatch):
    """The `places` table IS the queue, so a row that stays 'pending' after being processed
    would be handed back forever. Give up after a few fruitless passes rather than pin a
    core: a hot infinite loop is a far worse failure than a lane that reports an error."""
    job_id, job = _mk_job(db)

    async def _noop_enrich(store, rows, concurrency, country, on_progress, should_stop):
        return {"done": 0, "no_website": 0, "failed": 0, "thin": 0}   # leaves rows pending

    import webscraper.enrich as enrich_mod
    monkeypatch.setattr(enrich_mod, "enrich_places", _noop_enrich)

    def discovery(lane):
        _add_place(db, job_id, "stuck-1")
        return L.R_COMPLETED

    pipe = L.Pipeline(job_id, job, discovery, store_factory=db)   # the REAL EnrichmentLane
    start = time.monotonic()
    reasons = pipe.run()

    assert time.monotonic() - start < 15, "enrichment lane spun instead of bailing"
    assert reasons["enrichment"].startswith("error:"), reasons["enrichment"]
    s = db()
    assert s.lanes(job_id)["enrichment"]["ok"] is False
    s.close()


def test_whatsapp_lane_waits_out_an_open_login_instead_of_giving_up(db, monkeypatch):
    """W77: linking a second number from the CRM while a job runs used to END the job's
    WhatsApp lane — the rotation reached the account being scanned, _ensure_session raised
    WaNotLoggedIn("a WhatsApp login is open"), and the lane returned wa_not_logged_in for
    good. The lane must pause while the login window is open and pick up where it was."""
    from webscraper import wa_verify
    from webscraper.store import Store

    job_id, job = _mk_job(db, do_enrich=0, do_wa_verify=1)
    _add_place(db, job_id, "p1", "+919999999999")
    state = {"login_open": True, "calls": 0, "pending": True}
    row = {"place_key": "p1", "name": "biz p1", "number": "919999999999", "source": "maps"}

    monkeypatch.setattr(Store, "pending_wa_verify",
                        lambda self, jid, limit=25: [row] if state["pending"] else [])
    monkeypatch.setattr(Store, "count_wa_pending", lambda self, jid: 1 if state["pending"] else 0)
    def login_open(name=None):
        # Open while the lane raises and for its first wait poll; the scan finishes after.
        state["polls"] = state.get("polls", 0) + 1
        if state["polls"] > 2:
            state["login_open"] = False
        return state["login_open"]
    monkeypatch.setattr(wa_verify, "login_in_progress", login_open)

    def fake_verify(store, batch, on_progress, should_stop, job_id=None, headless=None, account=None):
        state["calls"] += 1
        if state["login_open"]:
            raise wa_verify.WaNotLoggedIn("[spare1] a WhatsApp login is open on this machine — waiting for it")
        on_progress("p1", "yes", "919999999999", "maps")
        state["pending"] = False
        return {"yes": 1, "no": 0, "unknown": 0}
    monkeypatch.setattr(wa_verify, "verify_places", fake_verify)
    monkeypatch.setattr(L, "IDLE_POLL_SEC", 0.01)

    pipe = L.Pipeline(job_id, job, lambda lane: L.R_COMPLETED, store_factory=db)
    pipe.discovery.done.set()
    lane = pipe.whatsapp
    lane.store = db()
    try:
        reason = lane._work()
    finally:
        lane.store.close()

    assert reason == L.R_COMPLETED, reason
    assert state["calls"] == 2
    s = db()
    msgs = [r["message"] for r in s.logs(job_id)]
    s.close()
    assert any("paused" in m and "login" in m for m in msgs), msgs
