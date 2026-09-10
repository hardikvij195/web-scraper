"""W99 — an 'unknown' WhatsApp verdict is re-offered exactly once (CRM T536).

W97 meant to settle a number after two `wa_checks` rows, but `record_wa_check` UPSERTs on
(job_id, place_key, number), so a second look overwrote the first row and the 2-row cap never
fired: the lane re-offered every unknown on every poll. The cap is now the `checks` counter
that the upsert bumps, and the `wa_numbers` summary carries it so the CRM applies the same rule.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from webscraper.models import Place
from webscraper.store import Store, now_iso


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "w99.db"
    return lambda: Store(path)


def _seed(db) -> tuple[Store, int]:
    s = db()
    jid = s.create_job(query="dentist", location="Pune", max_places=10, delay_sec=0)
    s.upsert_place(Place(job_id=jid, place_key="p1", name="P1", phone="+919876543210",
                         phone_digits="919876543210", country="IN",
                         detail_status="done", scraped_at=now_iso()))
    return s, jid


def test_unknown_is_offered_once_more_then_settled(db):
    s, jid = _seed(db)
    assert [r["number"] for r in s.pending_wa_verify(jid, 25)] == ["+919876543210"]

    s.record_wa_check(jid, "p1", "+919876543210", "maps", "unknown", "acc1")
    # one check, unknown → offered again (W97) …
    assert [r["number"] for r in s.pending_wa_verify(jid, 25)] == ["+919876543210"]
    assert s.count_wa_pending(jid) == 1
    checks = s.wa_checks(jid, "p1")
    assert len(checks) == 1 and checks[0]["checks"] == 1
    summary = json.loads(s.places(jid)[0]["wa_numbers"])
    assert summary == [{"number": "+919876543210", "source": "maps", "verdict": "unknown", "checks": 1}]

    s.record_wa_check(jid, "p1", "+919876543210", "maps", "unknown", "acc2")
    # … still one ROW (the upsert), but two CHECKS → settled, never re-offered.
    assert s.pending_wa_verify(jid, 25) == []
    assert s.count_wa_pending(jid) == 0
    checks = s.wa_checks(jid, "p1")
    assert len(checks) == 1 and checks[0]["checks"] == 2 and checks[0]["account"] == "acc2"
    summary = json.loads(s.places(jid)[0]["wa_numbers"])
    assert summary[0]["checks"] == 2 and summary[0]["verdict"] == "unknown"
    assert s.places(jid)[0]["wa_verified"] == "unknown"

    # A third look (the CRM's deliberate Re-verify) keeps counting and can still decide.
    s.record_wa_check(jid, "p1", "+919876543210", "maps", "yes", "acc2")
    assert s.wa_checks(jid, "p1")[0]["checks"] == 3
    assert s.places(jid)[0]["wa_verified"] == "yes"
    s.close()


def test_decided_verdict_settles_on_first_check(db):
    s, jid = _seed(db)
    s.record_wa_check(jid, "p1", "+919876543210", "maps", "no", "acc1")
    assert s.pending_wa_verify(jid, 25) == []
    assert s.wa_checks(jid, "p1")[0]["checks"] == 1
    s.close()


def test_migration_adds_checks_to_an_old_wa_checks_table(tmp_path: Path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE wa_checks (job_id INTEGER NOT NULL, place_key TEXT NOT NULL, "
                 "number TEXT NOT NULL, source TEXT NOT NULL, verdict TEXT NOT NULL, "
                 "checked_at TEXT NOT NULL, account TEXT, PRIMARY KEY (job_id, place_key, number))")
    conn.execute("INSERT INTO wa_checks VALUES (1, 'p1', '+919876543210', 'maps', 'unknown', 'x', 'a')")
    conn.commit()
    conn.close()
    s = Store(path)
    cols = {r[1] for r in s.conn.execute("PRAGMA table_info(wa_checks)")}
    assert "checks" in cols
    # a pre-W99 unknown row counts as checked once, so it gets its one retry
    assert s.wa_checks(1, "p1")[0]["checks"] == 1
    s.close()
