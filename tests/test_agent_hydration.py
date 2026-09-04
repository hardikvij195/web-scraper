"""T359 — "resume job on some other system": an agent that claims a re-enrich/both
re-run for a job it has never run before must rebuild a local `places` mirror from the
CRM's already-synced results, so `pending_enrichment()` (a local sqlite query) has
something to find. `discovery_pending` re-runs are deliberately NOT covered — see
`_hydrate_enrichment_from_cloud`'s docstring for why (job_links never syncs).

Everything runs against a temp SQLite file, never `data/leads.db`, and never touches
the network — `cloud` is a fake with a canned `.results()`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from webscraper.agent import _hydrate_enrichment_from_cloud
from webscraper.store import Store


@pytest.fixture()
def store(tmp_path: Path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


class _FakeCloud:
    """Stands in for CrmCloud — only `.results()` is used by the function under test."""
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def results(self, jid: int) -> list[dict]:
        return self._rows


# A realistic shape of what the CRM's narrow `results` action actually returns
# (supabase/functions/lead-finder-agent/index.ts: place_key,name,phone,whatsapp_number,
# whatsapp_source,wa_verified,country,website,email,emails,enrich_status,enrich_error,
# maps_url) — deliberately missing lat/lng/category/socials/etc.
_CLOUD_ROWS = [
    {"place_key": "p1", "name": "Pending Cafe", "phone": "+61200000001", "website": "https://pending.example",
     "email": None, "emails": [], "whatsapp_number": None, "whatsapp_source": None,
     "enrich_status": "pending", "enrich_error": None, "country": "AU", "maps_url": "https://maps/p1"},
    {"place_key": "p2", "name": "Done Diner", "phone": "+61200000002", "website": "https://done.example",
     "email": "hi@done.example", "emails": ["hi@done.example"], "whatsapp_number": "+61200000002",
     "whatsapp_source": "maps_link", "enrich_status": "done", "enrich_error": None,
     "country": "AU", "maps_url": "https://maps/p2"},
    {"place_key": "p3", "name": "Failed Factory", "phone": None, "website": "https://blocked.example",
     "email": None, "emails": [], "whatsapp_number": None, "whatsapp_source": None,
     "enrich_status": "failed", "enrich_error": "http_403", "country": "AU", "maps_url": "https://maps/p3"},
]


def test_hydrates_every_result_row_into_local_places(store: Store):
    job_id = store.create_job(query="q", location="here", max_places=10, delay_sec=0)
    n = _hydrate_enrichment_from_cloud(_FakeCloud(_CLOUD_ROWS), store, job_id, cloud_job_id=999)
    assert n == 3
    rows = {r["place_key"]: dict(r) for r in
            store.conn.execute("SELECT * FROM places WHERE job_id=?", (job_id,))}
    assert set(rows) == {"p1", "p2", "p3"}
    assert rows["p2"]["email"] == "hi@done.example"
    assert rows["p2"]["whatsapp_number"] == "+61200000002"


def test_only_the_pending_row_is_offered_for_enrichment(store: Store):
    """The whole point: EnrichmentLane's local pending_enrichment() query must pick up
    exactly the not-yet-done row, same as if this machine had scraped it itself."""
    job_id = store.create_job(query="q", location="here", max_places=10, delay_sec=0)
    _hydrate_enrichment_from_cloud(_FakeCloud(_CLOUD_ROWS), store, job_id, cloud_job_id=999)
    pending = store.pending_enrichment(job_id, limit=10)
    assert [p["place_key"] for p in pending] == ["p1"]


def test_detail_status_defaults_done_so_hydrated_rows_are_not_stuck_as_stubs(store: Store):
    """A W26 stub (detail_status='pending') is invisible to pending_enrichment() until
    the opener fills it in — a hydrated row must NOT look like a stub, since nothing
    will ever come along to fill it on this machine."""
    job_id = store.create_job(query="q", location="here", max_places=10, delay_sec=0)
    _hydrate_enrichment_from_cloud(_FakeCloud(_CLOUD_ROWS[:1]), store, job_id, cloud_job_id=999)
    row = store.conn.execute("SELECT detail_status FROM places WHERE job_id=? AND place_key='p1'",
                              (job_id,)).fetchone()
    assert (row["detail_status"] or "done") == "done"


def test_enrich_error_lands_even_though_its_not_a_place_col(store: Store):
    job_id = store.create_job(query="q", location="here", max_places=10, delay_sec=0)
    _hydrate_enrichment_from_cloud(_FakeCloud(_CLOUD_ROWS), store, job_id, cloud_job_id=999)
    row = store.conn.execute("SELECT enrich_error FROM places WHERE job_id=? AND place_key='p3'",
                              (job_id,)).fetchone()
    assert row["enrich_error"] == "http_403"


def test_no_results_is_a_harmless_noop(store: Store):
    job_id = store.create_job(query="q", location="here", max_places=10, delay_sec=0)
    n = _hydrate_enrichment_from_cloud(_FakeCloud([]), store, job_id, cloud_job_id=999)
    assert n == 0
    assert store.conn.execute("SELECT COUNT(*) FROM places WHERE job_id=?", (job_id,)).fetchone()[0] == 0


def test_missing_fields_stay_null_not_empty_string(store: Store):
    """Guards the actual safety property this whole feature leans on: `_flat()` drops
    None/"" before a sync, so a missing field must come back as NULL from sqlite, never
    as an empty string that `_flat()` would ALSO drop (harmless either way here, but a
    stray '' would be wrong data if anything ever reads it directly)."""
    job_id = store.create_job(query="q", location="here", max_places=10, delay_sec=0)
    _hydrate_enrichment_from_cloud(_FakeCloud(_CLOUD_ROWS[:1]), store, job_id, cloud_job_id=999)
    row = dict(store.conn.execute("SELECT * FROM places WHERE job_id=? AND place_key='p1'", (job_id,)).fetchone())
    for col in ("lat", "lng", "category", "instagram", "facebook", "summary"):
        assert row[col] is None, f"{col} should be NULL, got {row[col]!r}"
