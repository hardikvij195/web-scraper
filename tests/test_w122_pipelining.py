"""W122 (CRM T784/T786): pipelining across jobs + job priority.

  * `StageGate` — FIFO slot(s) shared by one stage (enrichment/whatsapp) across jobs.
  * `Store.queued_jobs()` orders by priority desc, then id.
  * `Worker` starts the next job's discovery the moment the current job's discovery slot
    frees, while capping total jobs in flight at `MAX_INFLIGHT_JOBS`.
  * `Worker.current_job` / `capacity()` back-compat + new shape.
  * `healthcheck.run_checks()` carries `memory`/`chrome`/`load` and never raises.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import webscraper.server as server_mod
from webscraper import lanes as L
from webscraper.store import Store


# ── (a) StageGate ────────────────────────────────────────────────────────────────────
def test_stage_gate_fifo_second_waits_then_acquires():
    gate = L.StageGate("enrichment", 1)
    assert gate.acquire(1, lambda: False, lambda m: None) is True

    notes: list[str] = []
    result: dict[str, bool] = {}

    def _acquire_job2() -> None:
        result["ok"] = gate.acquire(2, lambda: False, lambda m: notes.append(m))

    t = threading.Thread(target=_acquire_job2, daemon=True)
    t.start()
    time.sleep(0.3)
    assert t.is_alive(), "job 2 should still be waiting for job 1's slot"
    assert notes and "waiting for the enrichment slot" in notes[0] and "#1" in notes[0]

    gate.release(1)
    t.join(timeout=5)
    assert result.get("ok") is True
    gate.release(2)


def test_stage_gate_stopped_returns_false():
    gate = L.StageGate("whatsapp", 1)
    assert gate.acquire(1, lambda: True, lambda m: None) is False


# ── (b) Store.queued_jobs() priority order ──────────────────────────────────────────
def test_queued_jobs_orders_by_priority_desc_then_id(tmp_path: Path):
    s = Store(tmp_path / "test.db")
    a = s.create_job(query="a", location=None, max_places=10, delay_sec=0, phase="queued", priority=0)
    b = s.create_job(query="b", location=None, max_places=10, delay_sec=0, phase="queued", priority=5)
    c = s.create_job(query="c", location=None, max_places=10, delay_sec=0, phase="queued", priority=5)
    d = s.create_job(query="d", location=None, max_places=10, delay_sec=0, phase="queued", priority=1)
    ids = [int(r["id"]) for r in s.queued_jobs()]
    assert ids == [b, c, d, a]
    s.close()


# ── (c) Worker scheduling ────────────────────────────────────────────────────────────
class _FakePipe:
    def __init__(self) -> None:
        self.discovery_done = threading.Event()

    def discovery_slot_free(self) -> bool:
        return self.discovery_done.is_set()


def _wait_until(cond, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


@pytest.fixture()
def _patched_worker(tmp_path, monkeypatch):
    db_path = tmp_path / "sched.db"
    monkeypatch.setattr(server_mod, "Store", lambda *a, **kw: Store(db_path))
    monkeypatch.setattr(server_mod, "MAX_INFLIGHT_JOBS", 3)

    pipes: dict[int, _FakePipe] = {}
    finish_events: dict[int, threading.Event] = {}

    def _fake_run_job(self, job) -> None:
        job_id = int(job["id"])
        pipe = pipes[job_id]
        # Take it off `queued_jobs()` immediately, like the real `_run_job` does (via
        # `phase="scraping"`) — otherwise the scheduler would re-pick this same job the
        # moment its thread exits, instead of moving on to the next queued one.
        st = Store(db_path)
        try:
            st.update_job(job_id, phase="scraping")
        finally:
            st.close()
        with self._lock:
            self._inflight[job_id] = pipe
        finish_events[job_id].wait(timeout=10)
        with self._lock:
            self._inflight.pop(job_id, None)
            if self._disc_job == job_id:
                self._disc_job = None

    monkeypatch.setattr(server_mod.Worker, "_run_job", _fake_run_job)

    w = server_mod.Worker()
    seed = Store(db_path)
    job_ids = []
    for i in range(4):
        jid = seed.create_job(query=f"q{i}", location=None, max_places=10, delay_sec=0, phase="queued")
        pipes[jid] = _FakePipe()
        finish_events[jid] = threading.Event()
        job_ids.append(jid)
    seed.close()

    w.start()
    yield w, job_ids, pipes, finish_events

    for ev in finish_events.values():
        ev.set()
    w.wake.set()


def test_worker_pipelines_discovery_across_jobs_within_inflight_cap(_patched_worker):
    w, job_ids, pipes, finish_events = _patched_worker
    j1, j2, j3, j4 = job_ids

    # Only job 1 starts at first: its discovery slot is open.
    assert _wait_until(lambda: w._disc_job == j1)
    assert w.inflight_jobs() == [j1]

    # Job 2 must NOT start while job 1's discovery is still going.
    time.sleep(0.3)
    assert j2 not in w.inflight_jobs()

    # Free job 1's discovery slot -> job 2 starts (job 1 keeps running: enrichment/WA).
    pipes[j1].discovery_done.set()
    w.wake.set()
    assert _wait_until(lambda: w._disc_job == j2)
    assert j1 in w.inflight_jobs(), "job 1 must keep running after its discovery ends"

    # Free job 2's discovery -> job 3 starts (now 3 in flight: the MAX_INFLIGHT_JOBS cap).
    pipes[j2].discovery_done.set()
    w.wake.set()
    assert _wait_until(lambda: w._disc_job == j3)
    assert sorted(w.inflight_jobs()) == [j1, j2, j3]

    # Free job 3's discovery too -> the discovery slot is now free, but job 4 must NOT
    # start: three jobs are already in flight (MAX_INFLIGHT_JOBS=3).
    pipes[j3].discovery_done.set()
    w.wake.set()
    assert _wait_until(lambda: w._disc_job is None)
    time.sleep(0.3)
    assert j4 not in w.inflight_jobs()
    assert sorted(w.inflight_jobs()) == [j1, j2, j3]

    # Finish job 1 entirely (its thread exits) -> a slot frees -> job 4 can start.
    finish_events[j1].set()
    w.wake.set()
    assert _wait_until(lambda: j4 in w.inflight_jobs())
    assert _wait_until(lambda: j1 not in w.inflight_jobs())


# ── (d) current_job / capacity() ─────────────────────────────────────────────────────
def test_current_job_and_capacity_semantics():
    w = server_mod.Worker()
    assert w.current_job is None
    assert w.capacity() == {"pipelining": True, "discovery_free": True,
                            "inflight": 0, "max_inflight": server_mod.MAX_INFLIGHT_JOBS}

    w._inflight[5] = None
    w._disc_job = 5
    assert w.current_job == 5
    assert w.inflight_jobs() == [5]
    cap = w.capacity()
    assert cap["discovery_free"] is False and cap["inflight"] == 1

    w._inflight[3] = None
    w._disc_job = None
    assert w.current_job == 3          # smallest in-flight job once discovery is idle
    assert w.inflight_jobs() == [3, 5]

    # Back-compat setter (older tests poke `current_job` directly to fake idle/busy).
    w.current_job = None
    assert w.current_job is None
    assert w.inflight_jobs() == []
    w.current_job = 9
    assert w.current_job == 9
    assert w.inflight_jobs() == [9]


# ── (e) healthcheck capacity signals ────────────────────────────────────────────────
def test_run_checks_has_capacity_keys_and_never_raises(monkeypatch):
    from webscraper import healthcheck as HC

    r = HC.run_checks()
    for key in ("memory", "chrome", "load"):
        assert key in r and isinstance(r[key], dict)
        assert "checks" != key  # top-level, not nested under "checks"
    assert set(r["memory"]) >= {"total_mb", "available_mb", "used_pct"}
    assert set(r["chrome"]) >= {"processes", "rss_mb"}
    assert "cpu_pct" in r["load"]

    def _boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(HC.subprocess, "run", _boom)
    monkeypatch.setattr(HC.os, "getloadavg", _boom, raising=False)
    r2 = HC.run_checks()  # must not raise even with every subprocess call broken
    for key in ("memory", "chrome", "load"):
        assert key in r2 and isinstance(r2[key], dict)


def test_w123_parked_wa_lane_gives_back_its_stage_slot(monkeypatch):
    """W123: a WhatsApp lane parked on `_wait_for_relink` releases the whatsapp gate so the next
    job's lane can run, and takes it back when the account is linked again."""
    import threading
    from webscraper import lanes

    lanes.reset_stage_gates()
    gate = lanes.STAGE_GATES["whatsapp"]
    assert gate.acquire(1, lambda: False, lambda m: None)

    relinked = {"v": False}

    class _Store:
        def wa_relinked_since(self, since):
            return relinked["v"]

    class _Ctl:
        def enrichment_finished(self):
            return False

    class _Lane:
        key = "whatsapp"
        job_id = 1
        ctl = _Ctl()

        def note(self, *a, **k):
            pass

        def stopped(self):
            return False

    monkeypatch.setattr(lanes, "WA_RELINK_POLL_SEC", 0.01)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", lanes._wait_for_relink(_Lane(), _Store(), RuntimeError("no accounts"))))
    t.start()
    # While job 1 is parked, job 2 can take the single whatsapp slot.
    assert gate.acquire(2, lambda: False, lambda m: None)
    gate.release(2)
    relinked["v"] = True
    t.join(timeout=5)
    assert out["r"] is True
    assert 1 in gate._holders
    gate.release(1)


def test_w124_progress_reports_gate_waits():
    """W124: a job queued behind another on a stage gate shows up in `waiting_on` (what the
    agent's progress builder ships so the CRM's stuck-job cron sees the row changing)."""
    from webscraper import lanes

    lanes.reset_stage_gates()
    gate = lanes.STAGE_GATES["enrichment"]
    assert gate.waiting_on(7) is None
    assert gate.acquire(5, lambda: False, lambda m: None)
    import threading
    t = threading.Thread(target=lambda: gate.acquire(7, lambda: False, lambda m: None))
    t.start()
    for _ in range(100):
        w = gate.waiting_on(7)
        if w:
            break
        import time
        time.sleep(0.01)
    assert w == {"behind": [5], "position": 1}
    gate.release(5)
    t.join(timeout=5)
    assert gate.waiting_on(7) is None
    gate.release(7)


def test_w126_running_lane_is_not_ended(tmp_path, monkeypatch):
    """W126: a lane that has started but not ended keeps the stall watchdog armed; only a lane
    with no start stamp (never asked for) counts as ended."""
    from webscraper import agent

    class _Store:
        def __init__(self, lanes):
            self._lanes = lanes

        def lanes(self, jid):
            return self._lanes

    running = {"discovery": {"started_at": None, "ended_at": None, "ok": None},
               "enrichment": {"started_at": "t", "ended_at": None, "ok": None},
               "whatsapp": {"started_at": "t", "ended_at": None, "ok": None}}
    assert agent._lanes_all_ended(_Store(running), 1) is False
    ended = {k: {**v, "ended_at": "t2" if v["started_at"] else None} for k, v in running.items()}
    assert agent._lanes_all_ended(_Store(ended), 1) is True


def test_w126_restart_when_killed_lane_hangs(monkeypatch):
    """W126: STALL_EXIT_SEC after the watchdog failed a job whose lanes never ended, the agent
    closes its browsers and exits (the supervisor restarts it)."""
    from webscraper import agent

    calls = []
    monkeypatch.setattr(agent, "_close_browsers", lambda *a, **k: calls.append("close"))
    monkeypatch.setattr(agent.os, "_exit", lambda code: calls.append(("exit", code)))
    monkeypatch.setattr(agent, "_lanes_all_ended", lambda store, jid: False)

    class _Worker:
        def inflight_jobs(self):
            return [9]

    class _Srv:
        worker = _Worker()

    class _Store:
        def log(self, *a, **k):
            pass

    agent._STALL_KILLED.clear()
    agent._STALL_KILLED[9] = agent.time.monotonic() - agent.STALL_EXIT_SEC - 1
    agent._restart_if_lane_hung(_Store(), _Srv())
    assert calls == ["close", ("exit", 3)]
    agent._STALL_KILLED.clear()
