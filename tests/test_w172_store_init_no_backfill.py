"""W172 (CRM T1047, DELL 2026-10-07 16:04): `Store()._migrate()` used to run
`UPDATE places SET detail_status='done' WHERE detail_status IS NULL` on EVERY construction —
a full-table write (no index on detail_status) that took the SQLite write lock while the lanes
were streaming, so a worker tick died with "database is locked". The backfill belongs to the
moment the column is created, nowhere else.
"""
from __future__ import annotations

import sqlite3

from webscraper.store import Store

BACKFILL = "UPDATE places SET detail_status='done' WHERE detail_status IS NULL"


def _trace_statements(monkeypatch, sink: list[str]) -> None:
    """Attach a trace callback to every connection the store opens."""
    real_connect = sqlite3.connect

    def connect(*a, **kw):
        conn = real_connect(*a, **kw)
        conn.set_trace_callback(lambda stmt: sink.append(" ".join(stmt.split())))
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)


def test_fresh_store_does_not_run_the_backfill_again(tmp_path, monkeypatch):
    stmts: list[str] = []
    _trace_statements(monkeypatch, stmts)
    Store(tmp_path / "w172.db")            # schema created by SCHEMA; detail_status already present
    stmts.clear()
    Store(tmp_path / "w172.db")            # a second Store on the same file (worker + web UI pattern)
    assert not any(s.startswith("UPDATE places SET detail_status") for s in stmts), stmts


def test_old_db_without_detail_status_is_backfilled_once(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    s = Store(path)
    # Simulate a pre-detail_status database: drop the column (SQLite >= 3.35) and add a row.
    s.conn.execute("ALTER TABLE places DROP COLUMN detail_status")
    s.conn.commit()
    s.conn.close()
    stmts: list[str] = []
    _trace_statements(monkeypatch, stmts)
    s2 = Store(path)                        # migration adds the column -> backfill runs exactly once
    assert sum(1 for x in stmts if x.startswith("UPDATE places SET detail_status")) == 1, stmts
    assert "detail_status" in {r[1] for r in s2.conn.execute("PRAGMA table_info(places)")}
    stmts.clear()
    Store(path)
    assert not any(x.startswith("UPDATE places SET detail_status") for x in stmts)
