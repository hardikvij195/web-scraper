"""W113 (CRM T624): generalize W111's lock retry from `update_job` to every write method.

job #279 (agent "4 - DELL", scraper 1.9.3, 2026-09-15 00:01:40 UTC) died in
`bump_wa_account` — an `UPDATE wa_accounts`, a table W111 never touched — 30 s after
`update_job` itself had already survived one "database is locked" on the same job. The
lock was transient (another of this agent's own connections held it); W111 only retried
`jobs` writes, so every other write method still took its lane down on the first hit.
`Store._write()` gives every write method the same rollback/backoff/retry `update_job` had.
"""
from __future__ import annotations

import sqlite3
import threading
import time as real_time

import pytest

from webscraper import store as store_mod
from webscraper.store import Store


class _LockingConn:
    """Wraps a real connection; the first `fails` statements starting with `like` raise `error`."""

    def __init__(self, real, fails, like="", error="database is locked"):
        self.real, self.fails, self.like, self.error, self.rollbacks = real, fails, like, error, 0

    def execute(self, sql, *a):
        if sql.startswith(self.like) and self.fails > 0:
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


def test_bump_wa_account_rides_out_a_locked_database(st):
    """The exact write job #279 died on — not `jobs`, so W111 alone never covered it."""
    st.add_wa_account("acct")
    real = st.conn
    st.conn = _LockingConn(real, fails=2, like="UPDATE wa_accounts SET sent_today")
    st.bump_wa_account("acct", "2026-09-15")
    st.conn = real
    assert st.list_wa_accounts()[0]["sent_today"] == 1


def test_bump_wa_account_gives_up_after_the_retries_and_raises(st):
    st.add_wa_account("acct")
    real = st.conn
    st.conn = _LockingConn(real, fails=store_mod.LOCK_RETRIES + 1, like="UPDATE wa_accounts SET sent_today")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        st.bump_wa_account("acct", "2026-09-15")
    st.conn = real


def test_lane_end_retries_too(st):
    """A second write method on `jobs`, but not `update_job` — proves the helper is generic,
    not a copy hand-pasted onto one more method."""
    jid = st.create_job(query="q", location="l", max_places=10, delay_sec=0)
    real = st.conn
    st.conn = _LockingConn(real, fails=1, like="UPDATE jobs SET wa_ended_at")
    st.lane_end(jid, "whatsapp", "completed")
    st.conn = real
    assert st.get_job(jid)["wa_reason"] == "completed"


def test_seed_wa_checks_commits_each_row(tmp_path, monkeypatch):
    """Before W113, `seed_wa_checks`'s loop never called `commit()` at all — rows only
    became durable if some later write on the SAME connection happened to commit them.
    A second connection to the same file must see the row without `st` writing again."""
    monkeypatch.setattr(store_mod.time, "sleep", lambda s: None)
    db = tmp_path / "leads.db"
    st = Store(db)
    jid = st.create_job(query="q", location="l", max_places=10, delay_sec=0)
    st.conn.execute("INSERT INTO places(job_id, place_key, name) VALUES (?,?,?)", (jid, "p1", "Acme"))
    st.conn.commit()
    n = st.seed_wa_checks(jid, "p1", [{"number": "+15551234567", "verdict": "yes", "checks": 2}])
    assert n == 1
    other = sqlite3.connect(db)
    row = other.execute("SELECT checks FROM wa_checks WHERE job_id=? AND place_key=?", (jid, "p1")).fetchone()
    other.close()
    st.close()
    assert row == (2,)


def test_write_rides_out_a_real_second_connection_holding_the_lock(tmp_path, monkeypatch):
    """Not a mock: a second real connection does `BEGIN IMMEDIATE` (what a lane mid-Playwright
    navigation with an open write would look like) and releases it shortly after — `_write`
    must retry past sqlite's own OperationalError and land the write once it lets go."""
    monkeypatch.setattr(store_mod, "LOCK_RETRY_SEC", 0.05)
    db = tmp_path / "leads.db"
    st = Store(db)
    st.conn.execute("PRAGMA busy_timeout=50")               # fail fast per attempt, not after 30s
    jid = st.create_job(query="q", location="l", max_places=10, delay_sec=0)

    blocker = sqlite3.connect(db, timeout=0, check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")

    def _release():
        real_time.sleep(0.15)
        blocker.commit()
        blocker.close()

    t = threading.Thread(target=_release)
    t.start()
    st.lane_start(jid, "discovery")                          # must retry, not raise
    t.join()
    assert st.get_job(jid)["scrape_started_at"]
    st.close()
