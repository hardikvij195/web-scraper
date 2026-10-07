"""W153 (CRM T1047, owner 2026-10-07): "each lane 1 job should run on each system and then it should be
queued back — logically you cannot run 2 WA jobs at the same time; same for Google Maps and webscraping".

  * every stage gate has ONE slot (`enrich_slots()` / `wa_slots()` == 1), whatever the CRM / .env say
  * no W135 round-robin: a lane runs to its end, `_slice_done` never yields
  * a job whose only remaining lane cannot take its gate is PARKED (`PARK_MSG`, stop_requested) instead of
    sitting "running" behind another job's lane; a job with another lane still working keeps waiting
  * `LANE_ONE_JOB_PER_LANE=0` restores the old behaviour

Fakes only: no Playwright, no Chrome.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from webscraper import lanes as L
from webscraper.store import Store


@pytest.fixture(autouse=True)
def _rule_on(monkeypatch):
    monkeypatch.delenv("LANE_ONE_JOB_PER_LANE", raising=False)
    monkeypatch.setattr(L.StageGate, "POLL_SEC", 0.005)
    monkeypatch.setattr(L, "PARK_GRACE_SEC", 0.0)             # W160 grace is wall-clock; tests skip it
    L.reset_stage_gates()
    yield
    L.reset_stage_gates()


# ── slots ────────────────────────────────────────────────────────────────────────────
def test_every_stage_gate_has_one_slot(monkeypatch):
    monkeypatch.setenv("LANE_SLOTS_ENRICHMENT", "3")
    monkeypatch.setenv("LANE_SLOTS_WHATSAPP", "4")
    assert L.one_job_per_lane()
    assert L.enrich_slots() == 1 and L.wa_slots() == 1
    L.reset_stage_gates()
    assert L.STAGE_GATES["enrichment"].slots == 1 and L.STAGE_GATES["whatsapp"].slots == 1
    monkeypatch.setenv("LANE_ONE_JOB_PER_LANE", "0")
    assert not L.one_job_per_lane()
    assert L.enrich_slots() == 3 and L.wa_slots() == 4


# ── no round-robin ───────────────────────────────────────────────────────────────────
def _lane(key="whatsapp", job_id=1, ctl=None):
    class _Ctl:
        def other_lanes_done(self, lane):
            return True

        def enrichment_finished(self):
            return True

    class _Store:
        def __init__(self):
            self.updates: list[dict] = []
            self.logs: list[str] = []

        def update_job(self, jid, **kw):
            self.updates.append(kw)

        def log(self, jid, lane, msg, level="info"):
            self.logs.append(msg)

        def count_wa_pending(self, jid):
            return 500

        def count_pending_enrichment(self, jid):
            return 500

    class _Lane:
        notes: list[str]

        def __init__(self):
            self.key = key
            self.job_id = job_id
            self.ctl = ctl or _Ctl()
            self.store = _Store()
            self.notes = []
            self._stop = False
            self._slot_parked = True
            self._idle_since = None

        def note(self, m, *a, **k):
            self.notes.append(m)

        def stopped(self):
            return self._stop

    lane = _Lane()
    # bind the real Lane methods under test onto the fake
    for name in ("_slice_done", "_remaining", "_park_due", "_park", "_acquire", "_slot_resume"):
        setattr(lane, name, getattr(L.Lane, name).__get__(lane))
    return lane


def test_slice_done_never_yields_under_the_rule(monkeypatch):
    gate = L.STAGE_GATES["whatsapp"]
    me, other = _lane(job_id=1), _lane(job_id=2)
    assert gate.acquire(1, lambda: False, lambda m: None)
    t = threading.Thread(target=lambda: gate.acquire(2, other.stopped, lambda m: None), daemon=True)
    t.start()
    time.sleep(0.05)
    assert gate.others_waiting(1)
    for _ in range(10):                                  # 250 numbers: far past YIELD_UNITS
        assert me._slice_done(25) is True
    assert gate.holds(1) and not gate.holds(2), "job 1 kept the slot; no turn was handed over"
    other._stop = True
    t.join(timeout=2)


# ── park instead of wait ─────────────────────────────────────────────────────────────
def test_lane_only_job_is_parked_when_its_gate_is_held():
    gate = L.STAGE_GATES["whatsapp"]
    assert gate.acquire(7, lambda: False, lambda m: None)         # job 7 runs its WhatsApp lane
    lane = _lane(job_id=8)
    t0 = time.monotonic()
    assert lane._acquire(gate) is False
    assert time.monotonic() - t0 < 2.0
    assert lane.store.updates and lane.store.updates[-1]["stop_requested"] == 1
    msg = lane.store.updates[-1]["message"]
    assert msg.startswith("parked by the lane rule (W153): the whatsapp lane on this machine is busy with job #7")
    assert lane.store.logs == [msg]
    assert not gate.holds(8) and gate.free() == 0 and 8 not in gate._queue


def test_a_job_with_another_lane_still_working_waits_instead():
    gate = L.STAGE_GATES["enrichment"]
    assert gate.acquire(7, lambda: False, lambda m: None)

    class _Ctl:
        def other_lanes_done(self, lane):
            return False                                           # its Maps lane is still running

        def enrichment_finished(self):
            return False
    lane = _lane(key="enrichment", job_id=8, ctl=_Ctl())
    out: dict = {}
    t = threading.Thread(target=lambda: out.setdefault("r", lane._acquire(gate)), daemon=True)
    t.start()
    time.sleep(0.1)
    assert t.is_alive() and not lane.store.updates, "still waiting, not parked"
    gate.release(7)
    t.join(timeout=2)
    assert out["r"] is True and gate.holds(8)


def test_slot_resume_parks_too():
    gate = L.STAGE_GATES["whatsapp"]
    assert gate.acquire(7, lambda: False, lambda m: None)
    lane = _lane(job_id=9)                                        # _slot_parked = True: gave the slot up idle
    assert lane._slot_resume() is False
    assert "parked by the lane rule" in lane.store.updates[-1]["message"]


def test_rule_off_waits_like_before(monkeypatch):
    monkeypatch.setenv("LANE_ONE_JOB_PER_LANE", "0")
    gate = L.STAGE_GATES["whatsapp"]
    assert gate.acquire(7, lambda: False, lambda m: None)
    lane = _lane(job_id=8)
    out: dict = {}
    t = threading.Thread(target=lambda: out.setdefault("r", lane._acquire(gate)), daemon=True)
    t.start()
    time.sleep(0.1)
    assert t.is_alive() and not lane.store.updates
    gate.release(7)
    t.join(timeout=2)
    assert out["r"] is True


def test_a_free_gate_is_taken_at_once():
    gate = L.STAGE_GATES["whatsapp"]
    lane = _lane(job_id=8)
    assert lane._acquire(gate) is True and gate.holds(8) and not lane.store.updates


# ── Pipeline.other_lanes_done with the real lanes ────────────────────────────────────
def test_pipeline_other_lanes_done(tmp_path: Path):
    path = tmp_path / "t.db"
    s = Store(path)
    job_id = s.create_job(query="q", location="here", max_places=10, delay_sec=0)
    s.close()
    job = {"do_enrich": 1, "do_research": 0, "do_wa_verify": 1, "country": "IN", "reenrich_only": 1}
    pipe = L.Pipeline(job_id, job, lambda lane: L.R_COMPLETED, store_factory=lambda: Store(path))
    assert not pipe.discovery.enabled(), "a re-enrich job has no Maps lane"
    # W160: an enrichment lane that holds no slot yet is "not working" — the job may park if the
    # WhatsApp gate is held by another job (PARK_GRACE_SEC gives the websites lane time to take a free slot)
    assert pipe.other_lanes_done(pipe.whatsapp) is True
    g = L.STAGE_GATES["enrichment"]
    assert g.acquire(job_id, lambda: False, lambda m: None)
    assert pipe.other_lanes_done(pipe.whatsapp) is False          # websites lane IS working now
    g.release(job_id)
    pipe.enrichment.done.set()
    assert pipe.other_lanes_done(pipe.whatsapp) is True
    # W160: an alive WhatsApp lane that holds NO slot (idle, waiting for numbers) is not working
    assert pipe.other_lanes_done(pipe.enrichment) is True
    gate = L.STAGE_GATES["whatsapp"]
    assert gate.acquire(job_id, lambda: False, lambda m: None)   # now it IS verifying numbers
    assert pipe.other_lanes_done(pipe.enrichment) is False
    gate.release(job_id)
    # Maps alive (enabled, not ended) always counts as working — no gate to hold
    full = L.Pipeline(job_id, {"do_enrich": 1, "do_research": 0, "do_wa_verify": 1, "country": "IN"},
                      lambda lane: L.R_COMPLETED, store_factory=lambda: Store(path))
    assert full.discovery.enabled() and full.other_lanes_done(full.enrichment) is False
    full.discovery.done.set()
    assert full.other_lanes_done(full.enrichment) is True


def test_park_grace_holds_off_the_park(monkeypatch):
    """W160: within PARK_GRACE_SEC a blocked lane waits (its sibling may be about to take a free slot)."""
    monkeypatch.setattr(L, "PARK_GRACE_SEC", 0.3)
    gate = L.STAGE_GATES["whatsapp"]
    assert gate.acquire(7, lambda: False, lambda m: None)
    lane = _lane(job_id=8)
    t0 = time.monotonic()
    assert lane._acquire(gate) is False
    assert time.monotonic() - t0 >= 0.3 and "parked by the lane rule" in lane.store.updates[-1]["message"]
