"""W209 (CRM T1490, 2026-10-31): website-crawl failure modes on the agent side.

  #7  a re-enrich spends a try only on rows that were crawled (failed / thin), never on `pending`;
  #6  a side heartbeat reaches the CRM while the main loop is busy pulling/rebuilding a big job;
  #5  no work counter moved for AGENT_COUNTER_STALL_SEC -> the agent restarts itself, log chatter
      or not;
  #11 an AI-research failure (or exception) never fails the enrichment lane / job.
"""
from __future__ import annotations

from webscraper import agent, lanes
from webscraper import lanes as L
from webscraper.server import reenrich_reset

from tests.test_w152_relink_reprobe import _RStore, _enrich_lane


# -- #7 ----------------------------------------------------------------------------------------
def test_pending_row_keeps_its_attempts():
    assert reenrich_reset({"enrich_status": "pending", "enrich_attempts": 1}) == {"enrich_status": "pending"}
    assert reenrich_reset({"enrich_status": "pending", "enrich_attempts": 0}) == {"enrich_status": "pending"}


def test_failed_and_thin_rows_spend_a_try():
    assert reenrich_reset({"enrich_status": "failed", "enrich_attempts": 1}) == \
        {"enrich_status": "pending", "enrich_attempts": 2}
    assert reenrich_reset({"enrich_status": "thin", "enrich_attempts": None}) == \
        {"enrich_status": "pending", "enrich_attempts": 1}


def test_three_requeues_leave_a_never_crawled_row_at_zero():
    row = {"enrich_status": "pending", "enrich_attempts": 0}
    for _ in range(3):                                            # 3 re-queues on 2026-10-31
        row.update(reenrich_reset(row))
    assert row["enrich_attempts"] == 0
    row["enrich_status"] = "failed"                               # first real crawl failed
    row.update(reenrich_reset(row))
    assert row == {"enrich_status": "pending", "enrich_attempts": 1}   # one more try left


# -- #6 ----------------------------------------------------------------------------------------
def test_heartbeat_due_decision():
    assert agent._heartbeat_due(100.0, 50.0, 45.0) is True
    assert agent._heartbeat_due(100.0, 60.0, 45.0) is False
    assert agent._heartbeat_due(1e6, 0.0, 0.0) is False            # 0 = off


def test_side_heartbeat_pings_only_when_the_crm_went_quiet(monkeypatch):
    pings: list = []

    class _C:
        def ping(self):
            pings.append(1)
            agent._LAST_CRM_OK[0] = 500.0
            return True

    monkeypatch.delenv("AGENT_HEARTBEAT_SEC", raising=False)
    monkeypatch.setattr(agent.CrmCloud, "_denied", 0)
    agent._LAST_CRM_OK[0] = 480.0
    assert agent._heartbeat_once(_C(), now=500.0) is False           # a call 20 s ago: quiet
    assert agent._heartbeat_once(_C(), now=530.0) is True            # 50 s of nothing: ping
    assert pings == [1] and agent._LAST_CRM_OK[0] == 500.0
    # a revoked token never pings; a failing ping never raises
    monkeypatch.setattr(agent.CrmCloud, "_denied", 3)
    assert agent._heartbeat_once(_C(), now=10_000.0) is False
    monkeypatch.setattr(agent.CrmCloud, "_denied", 0)

    class _Boom:
        def ping(self):
            raise OSError("network down")
    assert agent._heartbeat_once(_Boom(), now=10_000.0) is False


def test_ping_does_not_stamp_the_loop_watchdog(monkeypatch):
    class _R:
        status_code = 200

    class _Client:
        def __init__(self, *a, **k):
            self.headers = {"X-Agent-Token": "t"}

        def post(self, url, json):
            assert json["action"] == "config" and "device" in json
            return _R()

    monkeypatch.setattr(agent.httpx, "Client", _Client)
    c = agent.CrmCloud("https://x.example", "t")
    before = agent._LOOP_WD["ok"]
    assert c.ping() is True
    assert agent._LOOP_WD["ok"] == before


# -- #5 ----------------------------------------------------------------------------------------
def test_counter_stall_restarts_even_while_logs_keep_coming(monkeypatch, tmp_path):
    from webscraper.store import Store
    st = Store(tmp_path / "t.db")
    jid = st.create_job("dentist", "Delhi", 10, 0.0)
    st.update_job(jid, cloud_id=77, cloud_kind="crm", scraped_count=1, enrich_done=2, wa_verify_done=3)
    calls: list = []
    monkeypatch.setattr(agent, "_close_browsers", lambda *a, **k: calls.append("close"))
    monkeypatch.setattr(agent.os, "_exit", lambda code: calls.append(("exit", code)))
    monkeypatch.delenv("AGENT_COUNTER_STALL_SEC", raising=False)
    monkeypatch.setattr(lanes, "alive_lanes", lambda j: ["enrichment"] if j == jid else [])
    agent._GLOBAL_STALL[0] = None
    agent._COUNTER_STALL[0] = None
    lim = agent.COUNTER_STALL_SEC
    assert agent._restart_if_all_stalled(st, "crm", now=0.0) is False            # arms
    for t in range(60, int(lim), 60):                                           # DELL: chatter every min
        st.log(jid, "enrichment", f"still trying {t}")
        assert agent._restart_if_all_stalled(st, "crm", now=float(t)) is False
    st.log(jid, "enrichment", "still trying")
    assert agent._restart_if_all_stalled(st, "crm", now=lim + 1) is True
    assert calls == ["close", ("exit", 3)]
    logs = [r["message"] for r in st.conn.execute("SELECT message FROM job_logs WHERE job_id=?", (jid,))]
    assert any("no work output for 25 min" in m for m in logs)


def test_counter_stall_resets_on_progress_and_skips_discovery(monkeypatch, tmp_path):
    from webscraper.store import Store
    st = Store(tmp_path / "t.db")
    jid = st.create_job("dentist", "Delhi", 10, 0.0)
    st.update_job(jid, cloud_id=78, cloud_kind="crm", scraped_count=1, enrich_done=2, wa_verify_done=3)
    monkeypatch.setattr(agent, "_close_browsers", lambda *a, **k: None)
    monkeypatch.setattr(agent.os, "_exit", lambda code: (_ for _ in ()).throw(AssertionError("no restart")))
    monkeypatch.delenv("AGENT_COUNTER_STALL_SEC", raising=False)
    lim = agent.COUNTER_STALL_SEC
    # discovery collecting links: no counter moves, but that is not a stall
    monkeypatch.setattr(lanes, "alive_lanes", lambda j: ["discovery", "enrichment"] if j == jid else [])
    agent._GLOBAL_STALL[0] = agent._COUNTER_STALL[0] = None
    for t in range(0, int(2 * lim), 60):
        st.log(jid, "discovery", f"collected {t}")
        assert agent._restart_if_all_stalled(st, "crm", now=float(t)) is False
    # enrichment only, a counter moves just before the limit -> clock restarts
    monkeypatch.setattr(lanes, "alive_lanes", lambda j: ["enrichment"] if j == jid else [])
    agent._GLOBAL_STALL[0] = agent._COUNTER_STALL[0] = None
    agent._restart_if_all_stalled(st, "crm", now=0.0)
    st.update_job(jid, enrich_done=3)
    st.log(jid, "enrichment", "enriched 3")
    assert agent._restart_if_all_stalled(st, "crm", now=lim - 1) is False
    st.log(jid, "enrichment", "x")
    assert agent._restart_if_all_stalled(st, "crm", now=lim + 60) is False
    # 0 = off
    monkeypatch.setenv("AGENT_COUNTER_STALL_SEC", "0")
    st.log(jid, "enrichment", "y")
    assert agent._restart_if_all_stalled(st, "crm", now=10 * lim) is False


# -- #11 ---------------------------------------------------------------------------------------
def test_research_exception_never_escapes_the_lane(monkeypatch):
    from webscraper import research as R

    async def boom(*a, **k):
        raise RuntimeError("nvidia: could not reach the API")
    monkeypatch.setattr(R, "research_places", boom)
    store = _RStore(["k1", "k2"])
    lane = _enrich_lane(store)
    lane._research([{"place_key": "k1"}, {"place_key": "k2"}])              # must not raise
    assert lane._research_targets == 2 and lane.research_error is None


def test_research_outage_does_not_turn_the_lane_into_an_error():
    import inspect
    src = inspect.getsource(L.EnrichmentLane)
    assert 'return f"error:AI research' not in src
    assert "AI research incomplete" in src
