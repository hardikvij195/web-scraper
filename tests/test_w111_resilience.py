"""W111 (CRM T606): what went wrong across the 2026-09-14 UK clinic jobs.

1. job #81 (3 - ASUS): the discovery lane died on a progress write — "database is locked".
   Store.update_job now rolls back, waits and retries a lock (and nothing else).
2. jobs #169 / #191: cancelled in the CRM while their agents restarted, resumed anyway.
   _requeue_orphans now asks the CRM before resuming.
(3 — the Maps tile retry — lives in maps.run_scrape's Playwright loop; not unit-tested here.)
"""
from __future__ import annotations

import sqlite3

import httpx
import pytest

from webscraper import agent
from webscraper import store as store_mod
from webscraper.store import Store


class _LockingConn:
    """Wraps a real connection; the first `fails` UPDATEs on jobs raise `error`."""

    def __init__(self, real: sqlite3.Connection, fails: int, error: str = "database is locked"):
        self.real, self.fails, self.error, self.rollbacks = real, fails, error, 0

    def execute(self, sql, *a):
        if sql.startswith("UPDATE jobs") and self.fails > 0:
            self.fails -= 1
            raise sqlite3.OperationalError(self.error)
        return self.real.execute(sql, *a)

    def commit(self):
        return self.real.commit()

    def rollback(self):
        self.rollbacks += 1
        return self.real.rollback()

    def __getattr__(self, name):
        return getattr(self.real, name)


@pytest.fixture
def st(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod.time, "sleep", lambda s: None)
    s = Store(tmp_path / "leads.db")
    yield s
    s.close()


def _job(s: Store) -> int:
    return int(s.create_job(query="q", location="l", max_places=10, delay_sec=0))


def test_update_job_rides_out_a_locked_database(st):
    jid = _job(st)
    real = st.conn
    st.conn = _LockingConn(real, fails=2)
    st.update_job(jid, message="collecting places…")
    st.conn = real
    assert st.get_job(jid)["message"] == "collecting places…"


def test_update_job_gives_up_after_the_retries(st):
    jid = _job(st)
    real = st.conn
    st.conn = _LockingConn(real, fails=store_mod.LOCK_RETRIES + 1)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        st.update_job(jid, message="x")
    st.conn = real


def test_other_sqlite_errors_are_not_retried(st):
    jid = _job(st)
    real = st.conn
    conn = _LockingConn(real, fails=5, error="no such column: nope")
    st.conn = conn
    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        st.update_job(jid, message="x")
    st.conn = real
    assert conn.fails == 4                                 # exactly one attempt


class _Cloud:
    def __init__(self, cancelled: set[int], offline: bool = False):
        self.cancelled, self.offline, self.pings = cancelled, offline, []

    def progress(self, jid, phase, progress):
        self.pings.append(jid)
        if self.offline:
            raise httpx.ConnectError("offline")
        return jid in self.cancelled


def _mid_run(s: Store, cloud_id: int) -> int:
    jid = _job(s)
    s.update_job(jid, cloud_id=cloud_id, cloud_kind="crm", phase="scraping")
    return jid


def test_requeue_skips_a_job_cancelled_in_the_crm(st):
    keep, gone = _mid_run(st, 190), _mid_run(st, 191)
    cloud = _Cloud(cancelled={191})
    assert agent._requeue_orphans(st, "crm", cloud) == 1
    assert st.get_job(keep)["phase"] == "queued"
    g = st.get_job(gone)
    assert g["phase"] == "stopped" and g["note"] == "synced" and g["stop_requested"] == 1
    assert sorted(cloud.pings) == [190, 191]


def test_requeue_still_resumes_when_the_crm_is_unreachable(st):
    jid = _mid_run(st, 169)
    assert agent._requeue_orphans(st, "crm", _Cloud(cancelled={169}, offline=True)) == 1
    assert st.get_job(jid)["phase"] == "queued"


def test_requeue_without_a_cloud_behaves_as_before(st):
    jid = _mid_run(st, 12)
    assert agent._requeue_orphans(st, "crm") == 1
    assert st.get_job(jid)["phase"] == "queued"
