"""W200 (CRM T1440): one pooled WhatsApp worker per account survives the job boundary; idle ones close."""
from __future__ import annotations

import os

from webscraper import lanes as L, wa_verify


class _St:
    def __init__(self, path="db1"):
        self.path = path
        self.closed = False

    def close(self):
        self.closed = True


class _Sess:
    made: list = []

    def __init__(self):
        self.closed = 0
        _Sess.made.append(self)

    def close(self):
        self.closed += 1


def _pool(monkeypatch, t):
    monkeypatch.setattr(wa_verify, "WaSession", _Sess)
    _Sess.made = []
    return wa_verify.WaPool(now=lambda: t[0])


def test_second_job_reuses_the_same_session(monkeypatch):
    t = [0.0]
    pool = _pool(monkeypatch, t)
    seen = []
    for _ in range(2):                                   # two consecutive "jobs" on one account
        box = pool.run("a1", lambda st, s: seen.append(s) or "ok", lambda: _St())
        assert box["result"] == "ok"
    assert len(_Sess.made) == 1 and seen[0] is seen[1] and _Sess.made[0].closed == 0
    pool.close_all()
    assert _Sess.made[0].closed == 1


def test_idle_worker_closes_after_the_idle_limit(monkeypatch):
    t = [0.0]
    pool = _pool(monkeypatch, t)
    monkeypatch.setenv("WA_KEEP_OPEN_IDLE_MIN", "10")
    pool.run("a1", lambda st, s: None, lambda: _St())
    t[0] = 9 * 60
    assert pool.reap(mem_high=False) == []
    t[0] = 10 * 60 + 1
    assert pool.reap(mem_high=False) == ["a1"]
    pool._workers.clear()
    for _ in range(50):
        if _Sess.made[0].closed:
            break
        import time
        time.sleep(0.05)
    assert _Sess.made[0].closed == 1


def test_busy_worker_is_never_reaped_and_ram_high_closes_idle(monkeypatch):
    t = [0.0]
    pool = _pool(monkeypatch, t)
    w = pool.acquire("a1", lambda: _St())
    t[0] = 3600
    assert pool.reap(mem_high=True) == []               # busy: never closed under it
    pool.release("a1")
    assert pool.reap(mem_high=True) == ["a1"]           # idle + RAM high: closed now
    w.thread.join(5)


def test_other_database_gets_its_own_worker(monkeypatch):
    t = [0.0]
    pool = _pool(monkeypatch, t)
    w1 = pool.acquire("a1", lambda: _St("db1"), "db1")
    w1.run(lambda st, s: None)[1].wait(5)
    pool.release("a1")
    w2 = pool.acquire("a1", lambda: _St("db2"), "db2")
    assert w2 is not w1
    pool.release("a1")
    pool.close_all()


def test_idle_min_knob(monkeypatch):
    monkeypatch.delenv("WA_KEEP_OPEN_IDLE_MIN", raising=False)
    assert wa_verify.wa_keep_open_idle_min() == 10.0
    monkeypatch.setenv("WA_KEEP_OPEN_IDLE_MIN", "3")
    assert wa_verify.wa_keep_open_idle_min() == 3.0


def test_enrich_concurrency_knob(monkeypatch):
    monkeypatch.setattr(L, "_device_env", lambda n: {"ENRICH_CONCURRENCY": "50"}.get(n))
    assert L.enrich_concurrency() == 20
    monkeypatch.setattr(L, "_device_env", lambda n: {"ENRICH_CONCURRENCY": "10"}.get(n))
    assert L.enrich_concurrency() == 10
    monkeypatch.setattr(L, "_device_env", lambda n: None)
    assert L.enrich_concurrency() == max(1, min(20, int(L.settings.enrich_concurrency)))


def test_enrich_slots_under_the_lane_rule(monkeypatch):
    monkeypatch.delenv("LANE_SLOTS_ENRICHMENT", raising=False)
    monkeypatch.setenv("LANE_ONE_JOB_PER_LANE", "1")
    monkeypatch.setattr(L, "_device_env", lambda n: {"ENRICH_SLOTS": "2"}.get(n))
    assert L.one_job_per_lane() and L.enrich_slots() == 2 and L.wa_slots() == 1
    monkeypatch.setattr(L, "_device_env", lambda n: {"ENRICH_SLOTS": "9"}.get(n))
    assert L.enrich_slots() == 3
