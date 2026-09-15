"""W115 (CRM T646): the Maps collector's own `database is locked` handling.

job #323 (Dell agent, 1.9.3): 7 "database is locked" retries in `Store._write`, then
`maps.py`'s `except Exception` at the top of `_collect_links` caught the exhausted
`OperationalError` and ended the WHOLE collection — every tile still to search was lost, not
just the one that hit the lock. `_persist_tile` now retries a locked tile's persist beyond
`Store._write`'s own retries, and only gives up on that ONE tile (returning False) instead of
raising out of the collector.

Also: `Store.__init__` sets an explicit `busy_timeout` and `synchronous=NORMAL` — the standard
WAL pairing that shortens how long a writer holds the lock, so there is less for every other
connection on the same sqlite file to wait behind in the first place.
"""
from __future__ import annotations

import sqlite3

import pytest

from webscraper import maps
from webscraper.store import Store


def test_store_sets_busy_timeout_and_normal_sync(tmp_path):
    s = Store(tmp_path / "leads.db")
    try:
        busy = s.conn.execute("PRAGMA busy_timeout").fetchone()[0]
        sync = s.conn.execute("PRAGMA synchronous").fetchone()[0]
        assert busy >= 30000
        assert sync == 1   # NORMAL
    finally:
        s.close()


class _FlakyStore:
    """save_stub_places/save_links/mark_collect_step raise 'database is locked' `fails`
    times total across the three calls, then succeed."""

    def __init__(self, fails: int):
        self.fails = fails
        self.calls: list[str] = []

    def _maybe_fail(self, name: str) -> None:
        self.calls.append(name)
        if self.fails > 0:
            self.fails -= 1
            raise sqlite3.OperationalError("database is locked")

    def save_stub_places(self, job_id, cards, country):
        self._maybe_fail("save_stub_places")

    def save_links(self, job_id, cards):
        self._maybe_fail("save_links")

    def mark_collect_step(self, job_id, step):
        self._maybe_fail("mark_collect_step")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(maps.time, "sleep", lambda s: None)


def test_persist_tile_rides_out_a_transient_lock():
    st = _FlakyStore(fails=2)
    events: list[tuple] = []
    ok = maps._persist_tile(st, 1, ["card"], "IN", "step1", 3, 10,
                            lambda kind, data: events.append((kind, data)))
    assert ok is True
    assert any(k == "tile_persist_retry" for k, _ in events)
    assert not any(k == "tile_persist_failed" for k, _ in events)


def test_persist_tile_gives_up_on_just_this_tile_after_the_retries():
    st = _FlakyStore(fails=maps.PERSIST_RETRIES + 1)
    events: list[tuple] = []
    ok = maps._persist_tile(st, 1, ["card"], "IN", "step1", 3, 10,
                            lambda kind, data: events.append((kind, data)))
    assert ok is False
    assert any(k == "tile_persist_failed" for k, _ in events)


def test_persist_tile_does_not_raise_a_non_lock_error_forever():
    """A genuine schema/bind error must still surface after ONE attempt — only a lock is
    worth retrying (mirrors Store._write's own rule)."""
    class _Broken:
        def save_stub_places(self, *a):
            raise sqlite3.OperationalError("no such column: bogus")

        def save_links(self, *a):
            pass

        def mark_collect_step(self, *a):
            pass

    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        maps._persist_tile(_Broken(), 1, ["card"], "IN", "step1", 1, 1, lambda k, d: None)
