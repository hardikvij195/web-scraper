"""W136 (CRM T1015): an idle lane does not own a stage slot.

DELL, 2026-10-03 16:53 UTC → 2026-10-04 (21 h): job #21540's enrichment lane held the
enrichment slot while parked on "websites wait — WhatsApp first"; its WhatsApp lane queued
for the WhatsApp slot, held by job #21543's WhatsApp lane, which sat idle (nothing pending)
waiting for its enrichment feeder — queued behind #21540. Nobody worked; every lane logged
"waiting for the … slot — held by job #N" every 90 s and both watchdogs read that as life.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from webscraper import agent, lanes as L, wa_verify
from webscraper.store import Store, now_iso


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "test.db"
    return lambda: Store(path)


def _mk_job(new_store, **over) -> tuple[int, dict]:
    s = new_store()
    job_id = s.create_job(query="q", location="here", max_places=10, delay_sec=0)
    job = {"do_enrich": 1, "do_research": 0, "do_wa_verify": 1, "country": "IN", "reenrich_only": 1}
    job.update(over)
    s.close()
    return job_id, job


def _add_place(new_store, job_id: int, key: str, phone: str | None = None) -> None:
    s = new_store()
    s.conn.execute(
        "INSERT OR IGNORE INTO places(job_id, place_key, name, phone, enrich_status, scraped_at) "
        "VALUES (?,?,?,?, 'done', ?)", (job_id, key, f"biz {key}", phone, now_iso()))
    s.conn.commit()
    s.close()


def _one_slot_each(monkeypatch):
    monkeypatch.setenv("LANE_SLOTS_ENRICHMENT", "1")
    monkeypatch.setenv("LANE_SLOTS_WHATSAPP", "1")
    L.reset_stage_gates()
    monkeypatch.setattr(L.StageGate, "POLL_SEC", 0.005)
    monkeypatch.setattr(L, "IDLE_POLL_SEC", 0.01)


def _fake_wa(monkeypatch, calls: list):
    monkeypatch.setattr(wa_verify, "login_in_progress", lambda name=None: False)

    def fake_verify(store, batch, on_progress, should_stop, job_id=None, headless=None, account=None):
        for r in batch:
            calls.append((job_id, r["place_key"]))
            store.record_wa_check(job_id, r["place_key"], r["number"], r.get("source") or "maps", "yes")
            on_progress(r["place_key"], "yes", r["number"], r.get("source") or "maps")
        return {"yes": len(batch), "no": 0, "unknown": 0}
    monkeypatch.setattr(wa_verify, "verify_places", fake_verify)


def _join_all(pipes, timeout=8.0):
    deadline = time.monotonic() + timeout
    for p in pipes:
        for lane in p.lanes:
            lane.join(timeout=max(0.0, deadline - time.monotonic()))


def test_dell_deadlock_two_jobs_cross_holding_idle_slots(db, monkeypatch):
    """The exact DELL shape. Job A's WhatsApp lane holds the WhatsApp slot with nothing to
    check (its enrichment feeder is queued for the enrichment slot). Job B's enrichment lane
    holds the enrichment slot while it waits for B's WhatsApp lane to clear B's Maps number —
    and B's WhatsApp lane is queued behind A. Before W136 this never ended."""
    _one_slot_each(monkeypatch)
    calls: list = []
    _fake_wa(monkeypatch, calls)
    s = db()
    s.add_wa_account("acc1")
    s.set_wa_status("acc1", "logged_in")
    s.close()

    a_id, a_job = _mk_job(db)                      # A: nothing pending for WhatsApp yet
    b_id, b_job = _mk_job(db)
    _add_place(db, b_id, "b1", "+919999999991")    # B: one Maps number → "WhatsApp first" wait

    pa = L.Pipeline(a_id, a_job, lambda lane: L.R_COMPLETED, store_factory=db)
    pb = L.Pipeline(b_id, b_job, lambda lane: L.R_COMPLETED, store_factory=db)
    L._PIPELINES[a_id], L._PIPELINES[b_id] = pa, pb
    try:
        wa_gate, en_gate = L.STAGE_GATES["whatsapp"], L.STAGE_GATES["enrichment"]
        pa.discovery.start(); pb.discovery.start()               # disabled lanes: done at once
        pa.discovery.join(2); pb.discovery.join(2)
        pa.whatsapp.start()                                      # A takes the WhatsApp slot, idles
        for _ in range(400):
            if wa_gate.holds(a_id):
                break
            time.sleep(0.005)
        assert wa_gate.holds(a_id)
        pb.enrichment.start()                                    # B takes the enrichment slot, waits on WA
        for _ in range(400):
            if en_gate.holds(b_id):
                break
            time.sleep(0.005)
        assert en_gate.holds(b_id)
        pb.whatsapp.start()                                      # queued behind A
        pa.enrichment.start()                                    # queued behind B

        _join_all([pa, pb], timeout=8.0)
        alive = [f"{p.job_id}:{l.key}" for p in (pa, pb) for l in p.lanes if l.is_alive()]
        assert not alive, f"deadlock — still alive: {alive}"
        assert calls == [(b_id, "b1")]
        assert pb.enrichment.reason in (L.R_COMPLETED, L.R_NO_TARGETS)
        assert pa.whatsapp.reason == L.R_NO_TARGETS
        # Either idle holder may be the one to let go first (A's WhatsApp lane for B's
        # WhatsApp lane, or B's enrichment lane for A's) — both orders end the deadlock.
        logs = [r["message"] for r in db().conn.execute(
            "SELECT message FROM job_logs WHERE job_id IN (?, ?)", (a_id, b_id))]
        assert any("slot to the next job in line meanwhile" in m for m in logs), logs
    finally:
        L._PIPELINES.pop(a_id, None); L._PIPELINES.pop(b_id, None)
        s = db()
        for p in (pa, pb):
            s.update_job(p.job_id, stop_requested=1)
        s.close()
        _join_all([pa, pb], timeout=3.0)


def test_idle_lane_keeps_slot_briefly_when_nobody_waits(monkeypatch):
    """Between two batches a lone lane must not churn: no waiter → it keeps the slot for
    IDLE_SLOT_SEC, then gives it up; the moment a batch is in hand it takes one back."""
    _one_slot_each(monkeypatch)
    gate = L.STAGE_GATES["whatsapp"]
    lane = L.WhatsAppLane(7, {"do_wa_verify": 1}, ctl=None)      # type: ignore[arg-type]
    lane.store = None
    assert gate.acquire(7, lambda: False, lambda m: None)
    lane._slot_idle("x")
    assert gate.holds(7)                                          # nobody waits, grace not over
    monkeypatch.setattr(L, "IDLE_SLOT_SEC", 0.0)
    lane._slot_idle("x")
    assert not gate.holds(7) and lane._slot_parked
    assert lane._slot_resume() and gate.holds(7) and not lane._slot_parked
    # another job queued → released at once, no grace
    monkeypatch.setattr(L, "IDLE_SLOT_SEC", 999.0)
    t = threading.Thread(target=lambda: gate.acquire(8, lambda: False, lambda m: None), daemon=True)
    t.start()
    for _ in range(200):
        if gate.others_waiting(7):
            break
        time.sleep(0.005)
    lane._slot_idle("x")
    assert not gate.holds(7)
    t.join(2)
    assert gate.holds(8)
    gate.release(8)


def test_lane_states_reports_idle_and_cloud_phase_waits(monkeypatch):
    L.reset_stage_gates()

    class _Lane:
        def __init__(self, k, parked):
            self.key, self._slot_parked = k, parked
        def is_alive(self):
            return True

    class _P:
        lanes = [_Lane("enrichment", True), _Lane("whatsapp", True)]
    monkeypatch.setitem(L._PIPELINES, 9, _P())
    assert L.lane_states(9) == {"enrichment": "idle", "whatsapp": "idle"}
    row = {"id": 9, "phase": "scraping", "do_research": 0, "enrich_done": 0, "enrich_total": 0}
    assert agent._cloud_phase(row) == "waiting"


def test_global_stall_ignores_slot_wait_lines(monkeypatch, tmp_path):
    """The machine-wide watchdog must see through a narrated deadlock."""
    st = Store(tmp_path / "t.db")
    jid = st.create_job("dentist", "Delhi", 10, 0.0)
    st.update_job(jid, cloud_id=77, cloud_kind="crm", scraped_count=1, enrich_done=2, wa_verify_done=3)
    calls: list = []
    monkeypatch.setattr(agent, "_close_browsers", lambda *a, **k: calls.append("close"))
    monkeypatch.setattr(agent.os, "_exit", lambda code: calls.append(("exit", code)))
    monkeypatch.setattr(L, "alive_lanes", lambda j: ["enrichment", "whatsapp"] if j == jid else [])
    L.RELINK_WAITERS.clear()
    agent._GLOBAL_STALL[0] = None
    assert agent._restart_if_all_stalled(st, "crm", now=10.0) is False          # arms
    for i in range(13):                                                           # 90-s wait notes, 19.5 min
        st.log(jid, "whatsapp", "waiting for the whatsapp slot — held by job #110")
        st.log(jid, "enrichment", "nothing to crawl yet — handing the enrichment slot to the next job in line meanwhile")
        assert agent._restart_if_all_stalled(st, "crm", now=10.0 + 90.0 * (i + 1)) is False
    assert agent._restart_if_all_stalled(st, "crm", now=10.0 + agent.GLOBAL_STALL_SEC + 5) is True
    assert calls == ["close", ("exit", 3)]
    # a real line still resets the clock
    agent._GLOBAL_STALL[0] = None
    calls.clear()
    agent._restart_if_all_stalled(st, "crm", now=5000.0)
    st.log(jid, "whatsapp", "Foo · +91 → ON WhatsApp ✓")
    assert agent._restart_if_all_stalled(st, "crm", now=5000.0 + agent.GLOBAL_STALL_SEC + 5) is False
    assert calls == []
