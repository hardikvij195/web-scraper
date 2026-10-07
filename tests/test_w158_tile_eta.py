"""W158 (CRM T1047): the Maps lane carries a tile-based ETA while the collector still walks tiles but
every found place is already opened (done == total). MAC #22562, 2026-10-07: "189/189 · estimating…"
for 12 minutes while 33 tiles ran. Pure eta.py logic — dict rows, no sqlite."""
from __future__ import annotations

from webscraper import eta
from tests.test_eta_lanes import NOW, FakeStore, by_key, iso, job


def _row(**over):
    base = job(scrape_started_at=iso(600), scraped_count=189, links_found=189,
               enrich_started_at=iso(590), enrich_done=75, enrich_total=75,
               wa_started_at=iso(580), wa_ended_at=iso(10), wa_ok=1, wa_reason="completed")
    base.update(over)
    return base


def test_places_exhausted_tiles_remaining_gives_a_tile_eta():
    row = _row(tiles_done=13, tiles_total=33, tiles_started_at=iso(13 * 21))      # 21 s/tile so far
    d = by_key(eta.lanes(row, FakeStore(scraping=None), NOW))["discovery"]
    assert d["status"] == "running" and d["done"] == 189 and d["total"] == 189
    assert d["tiles_done"] == 13 and d["tiles_total"] == 33
    assert d["rate_source"] == "tiles" and d["estimating"] is False
    assert abs(d["eta_sec"] - 20 * 21) <= 2, d["eta_sec"]
    s = eta.summary(row, FakeStore(scraping=None), NOW) if hasattr(eta, "summary") else None
    if s is not None:
        assert s["eta_sec"] >= d["eta_sec"] - 2, "the job ETA follows the longest lane"


def test_before_the_first_tile_the_default_rate_is_used():
    row = _row(tiles_done=0, tiles_total=33, tiles_started_at=None)
    d = by_key(eta.lanes(row, FakeStore(scraping=None), NOW))["discovery"]
    assert d["eta_sec"] == round(33 * eta.DEFAULT_TILE_SEC) and d["rate_source"] == "tiles"


def test_all_tiles_done_or_unknown_falls_back_to_places():
    d = by_key(eta.lanes(_row(tiles_done=33, tiles_total=33, tiles_started_at=iso(700)),
                         FakeStore(scraping=None), NOW))["discovery"]
    assert d["rate_source"] != "tiles" and d["tiles_done"] == 33 and d["tiles_total"] == 33
    d2 = by_key(eta.lanes(_row(), FakeStore(scraping=None), NOW))["discovery"]
    assert d2["tiles_total"] is None and d2["rate_source"] != "tiles"


def test_places_eta_wins_when_it_is_the_longer_one():
    # 100 places still to open at 5 s each = 500 s > 2 tiles x 21 s = 42 s
    row = _row(scraped_count=89, links_found=189, tiles_done=31, tiles_total=33, tiles_started_at=iso(31 * 21))
    d = by_key(eta.lanes(row, FakeStore(scraping=5.0), NOW))["discovery"]
    assert d["rate_source"] != "tiles" and d["eta_sec"] >= 400


def test_other_lanes_have_no_tile_fields():
    out = by_key(eta.lanes(_row(tiles_done=1, tiles_total=3), FakeStore(), NOW))
    assert out["enrichment"]["tiles_total"] is None and out["whatsapp"]["tiles_done"] is None
