"""W104 (CRM T547): the re-verify path ships a per-number delta with a `checks` count, and a
hydrated mirror seeds its local `wa_checks` from the CRM's `wa_numbers` so settled numbers are
not re-offered and a lower count can never be written back."""
from __future__ import annotations

import pytest

from webscraper.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "leads.db")
    jid = int(s.create_job("q", "l", 10, 1.0, country="SG"))
    s.conn.execute(
        "INSERT INTO places(job_id, place_key, name, phone, country, enrich_status, detail_status) "
        "VALUES (?, 'pk1', 'Shop', '+6598760972', 'SG', 'done', 'done')", (jid,))
    s.conn.commit()
    return s, jid


def _offered(s: Store, jid: int) -> list[str]:
    return [r["number"] for r in s.pending_wa_verify(jid, 50)]


def test_seed_marks_decided_numbers_as_settled(store):
    s, jid = store
    assert _offered(s, jid) == ["+6598760972"]
    n = s.seed_wa_checks(jid, "pk1", [{"number": "+6598760972", "source": "maps", "verdict": "no", "checks": 1}])
    assert n == 1
    assert _offered(s, jid) == []          # a yes/no is settled after one look


def test_seed_unknown_once_is_offered_once_more_and_twice_is_settled(store):
    s, jid = store
    s.seed_wa_checks(jid, "pk1", [{"number": "+6598760972", "source": "maps", "verdict": "unknown", "checks": 1}])
    assert _offered(s, jid) == ["+6598760972"]   # W97: one more look
    s.seed_wa_checks(jid, "pk1", [{"number": "+6598760972", "source": "maps", "verdict": "unknown", "checks": 2}])
    assert _offered(s, jid) == []                 # T536: two looks = settled


def test_seed_never_lowers_a_local_count(store):
    s, jid = store
    s.record_wa_check(jid, "pk1", "+6598760972", "maps", "unknown")
    s.record_wa_check(jid, "pk1", "+6598760972", "maps", "unknown")     # local checks = 2
    s.seed_wa_checks(jid, "pk1", [{"number": "+6598760972", "verdict": "unknown", "checks": 1}])
    row = s.conn.execute("SELECT checks FROM wa_checks WHERE job_id=? AND place_key='pk1'", (jid,)).fetchone()
    assert int(row[0]) == 2


def test_seed_ignores_junk(store):
    s, jid = store
    assert s.seed_wa_checks(jid, "pk1", [{"number": "+12"}, {"verdict": "yes"}, "x", None]) == 0


def test_reverify_delta_shape():
    """The per-number delta the re-verify path sends with every verdict (agent.py `onp`)."""
    from webscraper.store import plus
    num, source, status = "6598760972", "wa_link", "unknown"
    delta = [{"number": plus(num), "source": source or "maps", "verdict": status, "checks": 1}]
    assert delta == [{"number": "+6598760972", "source": "wa_link", "verdict": "unknown", "checks": 1}]
