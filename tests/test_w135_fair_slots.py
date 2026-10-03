"""W135 (CRM T1011): configurable stage-gate slots, round-robin yielding across jobs,
honest cloud phase, and the per-minute liveness block on every non-terminal row."""
from __future__ import annotations

import threading
import time

from webscraper import agent, lanes


def test_gate_slots_from_device_settings(monkeypatch):
    monkeypatch.delenv("LANE_SLOTS_ENRICHMENT", raising=False)
    monkeypatch.delenv("LANE_SLOTS_WHATSAPP", raising=False)
    monkeypatch.setattr(agent, "DEVICE_NAME", "testdev")
    monkeypatch.setenv("ENRICH_SLOTS__TESTDEV", "9")           # clamped to 3
    monkeypatch.setenv("WA_SLOTS__TESTDEV", "0")               # clamped to 1
    lanes.reset_stage_gates()
    assert lanes.STAGE_GATES["enrichment"].slots == 3
    assert lanes.STAGE_GATES["whatsapp"].slots == 1
    # defaults: enrichment 2, WhatsApp = _wa_parallel() (here: wa_parallel__<device>)
    monkeypatch.delenv("ENRICH_SLOTS__TESTDEV")
    monkeypatch.delenv("WA_SLOTS__TESTDEV")
    monkeypatch.setenv("WA_PARALLEL__TESTDEV", "2")
    assert lanes.enrich_slots() == 2
    assert lanes.wa_slots() == 2
    # live resize keeps the holder
    g = lanes.STAGE_GATES["enrichment"]
    assert g.acquire(1, lambda: False, lambda m: None)
    lanes.apply_stage_slots()
    assert g.slots == 2 and g.waiting_on(1) is None
    g.release(1)


def test_round_robin_two_lanes_alternate_on_one_slot(monkeypatch):
    monkeypatch.setenv("LANE_SLOTS_ENRICHMENT", "1")
    lanes.reset_stage_gates()
    gate = lanes.STAGE_GATES["enrichment"]
    monkeypatch.setattr(lanes.StageGate, "POLL_SEC", 0.005)
    order: list[tuple[int, int]] = []
    lock = threading.Lock()

    def worker(jid: int) -> None:
        assert gate.acquire(jid, lambda: False, lambda m: None)
        try:
            for i in range(3):
                with lock:
                    order.append((jid, i))
                time.sleep(0.01)
                assert gate.yield_slot(jid, lambda: False, lambda m: None)
        finally:
            gate.release(jid)

    t1 = threading.Thread(target=worker, args=(1,))
    t1.start()
    for _ in range(200):                      # job 2 asks while job 1 holds the slot
        if 1 in gate._holders:
            break
        time.sleep(0.005)
    t2 = threading.Thread(target=worker, args=(2,))
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert len(order) == 6
    jobs = [j for j, _ in order]
    # not all of job 1 then all of job 2: at least one hand-over in each direction
    assert jobs != sorted(jobs)
    assert any(a == 1 and b == 2 for a, b in zip(jobs, jobs[1:]))
    assert any(a == 2 and b == 1 for a, b in zip(jobs, jobs[1:]))


def test_lone_holder_keeps_its_slot():
    lanes.reset_stage_gates()
    gate = lanes.STAGE_GATES["whatsapp"]
    assert gate.acquire(5, lambda: False, lambda m: None)
    assert gate.others_waiting(5) is False
    assert gate.yield_slot(5, lambda: False, lambda m: None)   # immediate, still holding
    assert 5 in gate._holders
    gate.release(5)


def test_cloud_phase_from_lane_states(monkeypatch):
    row = {"id": 42, "phase": "scraping", "do_research": 0, "enrich_done": 0, "enrich_total": 0}
    monkeypatch.setattr(lanes, "lane_states", lambda jid: {})
    assert agent._cloud_phase(row) == "scraping"              # no live lanes: stored phase
    monkeypatch.setattr(lanes, "lane_states", lambda jid: {"enrichment": "running", "whatsapp": "queued"})
    assert agent._cloud_phase(row) == "enriching"
    monkeypatch.setattr(lanes, "lane_states", lambda jid: {"discovery": "running", "enrichment": "running"})
    assert agent._cloud_phase(row) == "scraping"
    monkeypatch.setattr(lanes, "lane_states", lambda jid: {"whatsapp": "running"})
    assert agent._cloud_phase(row) == "verifying_wa"
    monkeypatch.setattr(lanes, "lane_states", lambda jid: {"enrichment": "queued", "whatsapp": "queued"})
    assert agent._cloud_phase(row) == "waiting"
    rrow = {**row, "do_research": 1, "enrich_done": 10, "enrich_total": 10}
    monkeypatch.setattr(lanes, "lane_states", lambda jid: {"enrichment": "running"})
    assert agent._cloud_phase(rrow) == "researching"
    assert agent._cloud_phase({"id": 1, "phase": "queued"}) == "queued"
    assert agent._cloud_phase({"id": 1, "phase": "done"}) == "done"


def test_alive_block_present_for_a_queued_row(monkeypatch):
    monkeypatch.setattr(agent.eta, "summarise", lambda row, store: {
        "phases": [], "lanes": {}, "eta_sec": None, "phase_eta_sec": None,
        "estimating": True, "budget_left_sec": None})
    base = {"id": 9, "phase": "queued", "scraped_count": 0, "links_found": 0, "enrich_done": 0,
            "enrich_total": 0, "research_done": 0, "research_total": 0,
            "wa_verify_done": 0, "wa_verify_total": 0}
    out = agent._local_progress(base, None)
    assert out["alive"] == {"minute": int(time.time() // 60), "lanes": []}
    assert "alive" not in agent._local_progress({**base, "phase": "done"}, None)
    # lane threads alive -> keys listed (registry populated by Pipeline.run)
    class _L:
        def __init__(self, k): self.key = k
        def is_alive(self): return True
    class _P:
        lanes = [_L("enrichment"), _L("whatsapp")]
    monkeypatch.setitem(lanes._PIPELINES, 9, _P())
    assert agent._local_progress({**base, "phase": "scraping"}, None)["alive"]["lanes"] == ["enrichment", "whatsapp"]


def test_global_stall_restarts_only_when_live_lanes_and_nothing_moves(monkeypatch, tmp_path):
    from webscraper.store import Store
    st = Store(tmp_path / "t.db")
    jid = st.create_job("dentist", "Delhi", 10, 0.0)
    st.update_job(jid, cloud_id=77, cloud_kind="crm", scraped_count=1, enrich_done=2, wa_verify_done=3)
    calls: list = []
    monkeypatch.setattr(agent, "_close_browsers", lambda *a, **k: calls.append("close"))
    monkeypatch.setattr(agent.os, "_exit", lambda code: calls.append(("exit", code)))
    agent._GLOBAL_STALL[0] = None
    # no live lanes -> never arms
    assert agent._restart_if_all_stalled(st, "crm", now=0.0) is False
    monkeypatch.setattr(lanes, "alive_lanes", lambda j: ["enrichment"] if j == jid else [])
    assert agent._restart_if_all_stalled(st, "crm", now=10.0) is False          # arms
    assert agent._restart_if_all_stalled(st, "crm", now=10.0 + agent.GLOBAL_STALL_SEC - 1) is False
    # a relink wait keeps it calm
    lanes.RELINK_WAITERS.add(jid)
    assert agent._restart_if_all_stalled(st, "crm", now=10.0 + agent.GLOBAL_STALL_SEC + 5) is False
    lanes.RELINK_WAITERS.discard(jid)
    # re-armed after the relink pause; a log line resets the clock
    agent._restart_if_all_stalled(st, "crm", now=100.0)
    st.log(jid, "job", "still here")
    assert agent._restart_if_all_stalled(st, "crm", now=100.0 + agent.GLOBAL_STALL_SEC + 5) is False
    assert agent._restart_if_all_stalled(st, "crm", now=200.0 + 2 * agent.GLOBAL_STALL_SEC) is True
    assert calls == ["close", ("exit", 3)]
    logs = [r["message"] for r in st.conn.execute("SELECT message FROM job_logs WHERE job_id=?", (jid,))]
    assert any("agent restarting: nothing moved for 20 min" in m for m in logs)
