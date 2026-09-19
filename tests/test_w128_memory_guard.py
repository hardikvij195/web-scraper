"""W128/W129: per-machine in-flight cap read live from the CRM + memory guard before
starting a new job.

  * `server.max_inflight_jobs()` precedence: `MAX_INFLIGHT__<DEVICE>` > `MAX_INFLIGHT_JOBS`
    > default 3, clamped 1..6.
  * `Worker` will not start a queued job while RAM is at/above `MEMORY_START_MAX_PCT`;
    `capacity()["discovery_free"]` reflects that.
  * `agent._refresh_cloud_config` only overwrites env names that came from the cloud.
"""
from __future__ import annotations

import os
import threading
import time

import pytest

import webscraper.server as server_mod
from webscraper import healthcheck as HC
from webscraper.store import Store


# ── (a) max_inflight_jobs() precedence + clamp ──────────────────────────────────────
def test_max_inflight_jobs_precedence_and_clamp(monkeypatch):
    from webscraper import agent as agent_mod

    monkeypatch.setattr(agent_mod, "DEVICE_NAME", "ASUS-1")
    monkeypatch.delenv("MAX_INFLIGHT__ASUS-1", raising=False)
    monkeypatch.delenv("MAX_INFLIGHT_JOBS", raising=False)

    # default
    assert server_mod.max_inflight_jobs() == 3

    # MAX_INFLIGHT_JOBS alone
    monkeypatch.setenv("MAX_INFLIGHT_JOBS", "5")
    assert server_mod.max_inflight_jobs() == 5

    # device-specific wins over the generic var
    monkeypatch.setenv("MAX_INFLIGHT__ASUS-1", "2")
    assert server_mod.max_inflight_jobs() == 2

    # clamp: below 1 -> 1, above 6 -> 6
    monkeypatch.setenv("MAX_INFLIGHT__ASUS-1", "0")
    assert server_mod.max_inflight_jobs() == 1
    monkeypatch.setenv("MAX_INFLIGHT__ASUS-1", "99")
    assert server_mod.max_inflight_jobs() == 6

    # garbage falls back to 3
    monkeypatch.setenv("MAX_INFLIGHT__ASUS-1", "nope")
    assert server_mod.max_inflight_jobs() == 3


# ── (b) memory_blocked() gates starting a new job ───────────────────────────────────
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
def _mem_worker(tmp_path, monkeypatch):
    db_path = tmp_path / "mem.db"
    monkeypatch.setattr(server_mod, "Store", lambda *a, **kw: Store(db_path))
    monkeypatch.setattr(server_mod, "max_inflight_jobs", lambda: 3)
    # bust the 20s memory cache each test
    monkeypatch.setattr(server_mod, "_MEM_CACHE", [0.0, None])

    pipes: dict[int, _FakePipe] = {}
    finish_events: dict[int, threading.Event] = {}

    def _fake_run_job(self, job) -> None:
        job_id = int(job["id"])
        pipe = pipes[job_id]
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

    def _make(pct: float):
        monkeypatch.setattr(HC, "_memory", lambda: {"used_pct": pct})

    w = server_mod.Worker()
    seed = Store(db_path)
    jid = seed.create_job(query="q", location=None, max_places=10, delay_sec=0, phase="queued")
    pipes[jid] = _FakePipe()
    finish_events[jid] = threading.Event()
    seed.close()

    yield w, jid, _make

    for ev in finish_events.values():
        ev.set()
    w.wake.set()


def test_worker_does_not_start_job_when_memory_high(_mem_worker, monkeypatch):
    w, jid, set_mem = _mem_worker
    set_mem(90.0)
    monkeypatch.setenv("MEMORY_START_MAX_PCT", "85")

    assert server_mod.memory_blocked() is True
    cap = w.capacity()
    assert cap["discovery_free"] is False
    assert cap["memory_pct"] == 90.0

    w.start()
    time.sleep(0.3)
    assert jid not in w.inflight_jobs()
    assert w._disc_job is None


def test_worker_starts_job_when_memory_low(_mem_worker, monkeypatch):
    w, jid, set_mem = _mem_worker
    set_mem(50.0)
    monkeypatch.setenv("MEMORY_START_MAX_PCT", "85")

    assert server_mod.memory_blocked() is False
    cap = w.capacity()
    assert cap["discovery_free"] is True

    w.start()
    assert _wait_until(lambda: w._disc_job == jid)
    assert jid in w.inflight_jobs()


# ── (c) _refresh_cloud_config only overwrites cloud-sourced env names ──────────────
def test_refresh_cloud_config_overwrites_only_cloud_env(monkeypatch):
    from webscraper import agent as agent_mod

    monkeypatch.setattr(agent_mod, "_CLOUD_ENV", {"MAX_INFLIGHT__ASUS-1"})
    monkeypatch.setattr(agent_mod, "_last_config_refresh", [0.0])
    monkeypatch.setenv("MAX_INFLIGHT__ASUS-1", "3")     # came from cloud at start
    monkeypatch.setenv("WA_PARALLEL__ASUS-1", "1")      # set locally — not in _CLOUD_ENV

    class _FakeCloud:
        def config(self):
            return {"max_inflight__asus-1": "2", "wa_parallel__asus-1": "4"}

    agent_mod._refresh_cloud_config(_FakeCloud(), force=True)

    assert os.environ["MAX_INFLIGHT__ASUS-1"] == "2"    # overwritten: was cloud-sourced
    assert os.environ["WA_PARALLEL__ASUS-1"] == "1"     # untouched: local value wins


def test_w130_crm_stop_is_not_reported_as_a_verdict(tmp_path):
    """W130: a job the CRM paused/cancelled must not send done('error') — the CRM has already
    moved that row on (a roll-update re-queues it one tick later)."""
    from pathlib import Path

    from webscraper import agent
    from webscraper.store import Store

    st = Store(Path(tmp_path) / "leads.db")
    jid = st.create_job(query="x", location="y", max_places=1, delay_sec=0)
    st.update_job(jid, message="stopped - the agent was parked from the CRM")
    assert agent._cancelled_by_crm(st, jid) is True

    jid2 = st.create_job(query="x", location="y", max_places=1, delay_sec=0)
    st.update_job(jid2, message="finished")
    assert agent._cancelled_by_crm(st, jid2) is False
    st.log(jid2, "job", "cancelled in the CRM while the agent was down - not resumed", "warn")
    assert agent._cancelled_by_crm(st, jid2) is True
    st.close()
