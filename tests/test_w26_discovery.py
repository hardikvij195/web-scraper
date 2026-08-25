"""W26 — concurrent discovery (collector + opener) and every-number WhatsApp checks.

Everything runs against a temp SQLite file, never `data/leads.db`. The run_scrape tests
replace Playwright with a fake so the two-thread orchestration (collector persists a tile,
opener fills it while the next tile is still loading) is exercised without a browser.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from webscraper import eta
from webscraper import maps
from webscraper.maps import FeedCard, Pacing
from webscraper.models import Place
from webscraper.store import Store, aggregate_wa, now_iso, wa_candidates


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "w26.db"
    return lambda: Store(path)


def _job(new_store) -> int:
    s = new_store()
    jid = s.create_job(query="dentist", location="Pune", max_places=10, delay_sec=0)
    s.close()
    return jid


def _card(key: str, **kw) -> FeedCard:
    base = dict(href=f"https://www.google.com/maps/place/x/data=!19s{key}", name=f"biz {key}",
                rating=4.5, reviews_count=120, lat=18.5, lng=73.8)
    base.update(kw)
    return FeedCard(**base)


# ── extractors ────────────────────────────────────────────────────────────────────────
def test_extract_phones_reads_tel_links_then_text_and_dedupes():
    from webscraper.extractors import extract_phones
    html = ('<script>var x="+44 20 7946 0000"</script>'
            '<a href="tel:+44 20 7636 5774">call</a><p>Ring 020 7946 0958 or 020 7636 5774</p>'
            '<span>order #1234567890</span>')
    assert extract_phones(html, "GB") == ["+442076365774", "+442079460958"]
    assert extract_phones("", "GB") == []


# ── stub → fill ───────────────────────────────────────────────────────────────────────
def test_stub_then_opener_fill_keeps_feed_values_via_coalesce(db):
    jid = _job(db)
    s = db()
    assert s.save_stub_places(jid, [_card("ChIJa")], "IN") == 1
    row = s.places(jid)[0]
    assert row["detail_status"] == "pending" and row["rating"] == 4.5 and row["phone"] is None
    assert s.save_stub_places(jid, [_card("ChIJa", rating=1.0)], "IN") == 0   # never touches an existing row
    assert s.places(jid)[0]["rating"] == 4.5

    # The headless panel exposes no rating / review count -> Place carries None for them.
    s.upsert_place(Place(job_id=jid, place_key="ChIJa", name="Biz A", phone="+919876543210",
                         website="https://a.example", rating=None, reviews_count=None,
                         detail_status="done", scraped_at=now_iso()))
    row = s.places(jid)[0]
    assert row["detail_status"] == "done"
    assert row["phone"] == "+919876543210" and row["website"] == "https://a.example"
    assert row["rating"] == 4.5 and row["reviews_count"] == 120, "COALESCE must keep the stub's values"
    assert row["lat"] == 18.5
    assert row["changed_at"], "the fill must bump changed_at so the agent streams it as an update (W20)"
    s.close()


def test_pending_enrichment_excludes_stubs(db):
    jid = _job(db)
    s = db()
    s.save_stub_places(jid, [_card("ChIJstub1"), _card("ChIJstub2")], "IN")
    s.upsert_place(Place(job_id=jid, place_key="ChIJstub1", name="A", website="https://a.example",
                         detail_status="done", scraped_at=now_iso()))
    keys = [r["place_key"] for r in s.pending_enrichment(jid, 10)]
    assert keys == ["ChIJstub1"]
    assert s.count_pending_enrichment(jid) == 1
    assert s.count_places_detailed(jid) == 1 and s.count_places(jid) == 2
    s.close()


def test_migration_marks_old_rows_detailed(db):
    jid = _job(db)
    s = db()
    s.conn.execute("INSERT INTO places(job_id, place_key, name, enrich_status, detail_status) "
                   "VALUES (?,?,?,'pending',NULL)", (jid, "old", "Old"))
    s.conn.commit()
    s.close()
    s = db()                       # re-open → _migrate runs
    assert s.places(jid)[0]["detail_status"] == "done"
    assert [r["place_key"] for r in s.pending_enrichment(jid)] == ["old"]
    s.close()


# ── every-number WhatsApp ─────────────────────────────────────────────────────────────
def test_wa_pending_offers_maps_phone_before_enrichment_and_site_numbers_after(db):
    jid = _job(db)
    s = db()
    s.upsert_place(Place(job_id=jid, place_key="p1", name="P1", phone="+919876543210",
                         phone_digits="919876543210", country="IN", website="https://p1.example",
                         detail_status="done", scraped_at=now_iso()))
    pend = s.pending_wa_verify(jid, 25)
    assert [(r["number"], r["source"]) for r in pend] == [("+919876543210", "maps")]
    assert s.count_wa_pending(jid) == 1

    s.record_wa_check(jid, "p1", "+919876543210", "maps", "no", "acc1")
    assert s.pending_wa_verify(jid, 25) == []
    assert s.places(jid)[0]["wa_verified"] == "no"

    # Enrichment lands: a wa.me link plus two site numbers, one of which IS the Maps phone.
    s.update_enrichment(jid, "p1", {"enrich_status": "done", "whatsapp_number": "+919999999999",
                                    "whatsapp_source": "wa_link",
                                    "site_phones": ["+912012345678", "+91 98765 43210"]})
    pend = s.pending_wa_verify(jid, 25)
    assert [(r["number"], r["source"]) for r in pend] == [("+919999999999", "wa_link"),
                                                          ("+912012345678", "site")]
    assert s.count_wa_pending(jid) == 2 and s.count_wa_done(jid) == 1

    s.record_wa_check(jid, "p1", "+912012345678", "site", "yes", "acc1")
    row = s.places(jid)[0]
    assert row["wa_verified"] == "yes" and row["whatsapp_number"] == "+912012345678"
    assert row["whatsapp_source"] == "verified"
    s.record_wa_check(jid, "p1", "+919999999999", "wa_link", "yes", "acc1")
    row = s.places(jid)[0]
    assert row["whatsapp_number"] == "+919999999999", "wa_link beats site for the promoted number"
    assert json.loads(row["wa_numbers"]) == [
        {"number": "+919876543210", "source": "maps", "verdict": "no"},
        {"number": "+912012345678", "source": "site", "verdict": "yes"},
        {"number": "+919999999999", "source": "wa_link", "verdict": "yes"},
    ]
    assert s.pending_wa_verify(jid, 25) == [] and s.count_wa_done(jid) == 3
    s.close()


def test_wa_candidates_dedupes_unverified_guess_against_maps_phone():
    row = {"phone": "+919876543210", "whatsapp_number": "+919876543210", "whatsapp_source": "unverified",
           "country": "IN", "enrich_status": "no_website", "site_phones": "[]"}
    assert wa_candidates(row) == [("+919876543210", "maps")]
    row2 = {"phone": "020 7946 0958", "country": "GB", "enrich_status": "pending",
            "site_phones": json.dumps(["+442071234567"])}
    assert wa_candidates(row2) == [("+442079460958", "maps")], "site numbers wait for enrichment"


def test_aggregate_wa_rules_and_unverified_clearing(db):
    assert aggregate_wa([]) == ("unknown", None)
    assert aggregate_wa([{"number": "+1", "source": "maps", "verdict": "unknown"}]) == ("unknown", None)
    assert aggregate_wa([{"number": "+11111111", "source": "maps", "verdict": "no"},
                         {"number": "+22222222", "source": "site", "verdict": "no"}]) == ("no", None)
    assert aggregate_wa([{"number": "+11111111", "source": "site", "verdict": "yes"},
                         {"number": "+22222222", "source": "maps", "verdict": "yes"}]) == ("yes", "+22222222")

    jid = _job(db)
    s = db()
    s.upsert_place(Place(job_id=jid, place_key="g", name="G", phone="+919876543210",
                         whatsapp_number="+919876543210", whatsapp_source="unverified",
                         detail_status="done", scraped_at=now_iso()))
    s.record_wa_check(jid, "g", "+919876543210", "maps", "no", "acc")
    row = s.places(jid)[0]
    assert row["wa_verified"] == "no" and row["whatsapp_number"] is None and row["whatsapp_source"] is None
    s.close()


def test_wa_line_names_the_source():
    from webscraper.lanes import _wa_line
    assert _wa_line({"name": "Cafe"}, "yes", "+4412345678", "maps") == "Cafe · +4412345678 (maps) → ON WhatsApp ✓"
    assert _wa_line({"name": "Cafe"}, "no", "+4412345678", "site") == "Cafe · +4412345678 (website) → not on WhatsApp ✗"
    assert _wa_line({"name": "Cafe"}, "unknown", None) == "Cafe · — → no number to check"


def test_verify_places_checks_every_number_and_records_per_number(db, monkeypatch):
    """The lane hands one number per row; each verdict lands in wa_checks and the place
    aggregate follows. Drives the real verify_places with a fake WhatsApp page."""
    from webscraper import wa_verify as wv
    jid = _job(db)
    s = db()
    s.add_wa_account("acc1")
    s.upsert_place(Place(job_id=jid, place_key="p1", name="P1", phone="+919876543210", country="IN",
                         detail_status="done", scraped_at=now_iso()))
    s.update_enrichment(jid, "p1", {"enrich_status": "done", "site_phones": ["+912012345678"]})
    verdicts = {"919876543210": "no", "912012345678": "yes"}

    class _Ctx:
        def close(self): pass

    class _Page:
        num = None

        def goto(self, url, **kw):
            self.num = url.split("phone=")[1].split("&")[0]

    class _PW:
        def start(self): return self

        def stop(self): pass

    monkeypatch.setattr(wv, "sync_playwright", lambda: _PW())
    monkeypatch.setattr(wv, "_ensure_session",
                        lambda pw, open_ctx, rl, name: open_ctx.setdefault(name, (_Ctx(), _Page()))[1])
    monkeypatch.setattr(wv, "_decide", lambda page: verdicts[page.num])
    monkeypatch.setattr(wv, "_dismiss_popup", lambda page: None)
    monkeypatch.setattr(wv.settings, "wa_delay_min", 0.0)
    monkeypatch.setattr(wv.settings, "wa_delay_max", 0.0)

    seen: list[tuple] = []
    rows = s.pending_wa_verify(jid, 25)
    assert len(rows) == 2
    res = wv.verify_places(s, rows, on_progress=lambda pk, st, num=None, src=None: seen.append((pk, st, num, src)),
                           job_id=jid)
    assert res["checked"] == 2 and res["yes"] == 1 and res["no"] == 1
    assert seen == [("p1", "no", "+919876543210", "maps"), ("p1", "yes", "+912012345678", "site")]
    row = s.places(jid)[0]
    assert row["wa_verified"] == "yes" and row["whatsapp_number"] == "+912012345678"
    assert s.pending_wa_verify(jid, 25) == [] and s.count_wa_done(jid) == 2
    s.close()


# ── ETA counts ────────────────────────────────────────────────────────────────────────
def test_live_counts_discovery_uses_detailed_places_and_unopened_links(db):
    jid = _job(db)
    s = db()
    cards = [_card("ChIJk1"), _card("ChIJk2"), _card("ChIJk3")]
    s.save_links(jid, cards)
    s.save_stub_places(jid, cards, "IN")
    s.mark_link_opened(jid, "ChIJk1")
    s.upsert_place(Place(job_id=jid, place_key="ChIJk1", name="K1", phone="+919876543210",
                         detail_status="done", scraped_at=now_iso()))
    row = dict(s.get_job(jid))
    row["disc_active"] = 1
    live = eta._live_counts(row, s)
    assert live["discovery"] == (1, 1, 2)
    assert live["whatsapp"] == (0, 0, 1)          # k1's Maps phone is checkable already
    assert live["enrichment"] == (0, 0, 0)        # k1 has no website; stubs are not enrichable
    s.close()


# ── run_scrape: collector + opener side by side ───────────────────────────────────────
class _FakePage:
    url = "https://www.google.com/maps/search/x"

    def set_default_timeout(self, *a): pass

    def goto(self, *a, **k): pass


class _FakeCtx:
    def __init__(self): self.pages = [_FakePage()]

    def route(self, *a, **k): pass

    def close(self): pass


class _FakePW:
    class chromium:
        @staticmethod
        def launch_persistent_context(**kw):
            return _FakeCtx()

    def __enter__(self): return self

    def __exit__(self, *a): pass


def _fake_playwright(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(maps, "sync_playwright", lambda: _FakePW())
    monkeypatch.setattr(maps.settings, "profile_dir", tmp_path / "browser-profile")
    monkeypatch.setattr(maps.random, "uniform", lambda a, b: 0.001)   # no multi-second Maps pacing in tests
    monkeypatch.setattr(maps, "OPENER_POLL_SEC", 0.05)                 # production idles 2 s; tiles here take 0.4 s


def test_opener_fills_first_tile_while_collector_still_tiling(db, monkeypatch, tmp_path):
    _fake_playwright(monkeypatch, tmp_path)
    jid = _job(db)
    store_path = db().path
    timeline: list[tuple[float, str, str]] = []
    t0 = time.monotonic()
    lock = threading.Lock()

    def mark(kind: str, what: str) -> None:
        with lock:
            timeline.append((round(time.monotonic() - t0, 3), kind, what))

    tiles = {"n": 0}

    def fake_collect(page, want, on_progress=None):
        tiles["n"] += 1
        i = tiles["n"]
        time.sleep(0.4)                                  # a slow tile
        mark("collect", f"tile {i}")                     # stamped on the COLLECTOR thread
        return [_card(f"ChIJt{i}a"), _card(f"ChIJt{i}b")]

    stub_seen_pending = {}

    def fake_scrape(page, href, job_id, country):
        key = href.split("!19s")[1]
        s = Store(store_path)                            # the opener thread's own peek
        row = next(r for r in s.places(job_id) if r["place_key"] == key)
        stub_seen_pending[key] = row["detail_status"]
        s.close()
        time.sleep(0.05)
        mark("place", key)
        return Place(job_id=job_id, place_key=key, name=None, phone="+919876543210",
                     website=f"https://{key}.example", scraped_at=now_iso())

    monkeypatch.setattr(maps, "collect_place_links", fake_collect)
    monkeypatch.setattr(maps, "scrape_place", fake_scrape)

    def on_event(kind, data):
        if kind == "tile":
            mark("tile", f"{data['tile']}/{data['tiles']} +{data['added']}")
        elif kind == "links_done":
            mark("links_done", str(data["count"]))

    s = db()
    saved = maps.run_scrape(s, jid, "cafe, bakery, dentist", "Pune", 0, Pacing(delay_sec=0.001, pause_every=0),
                            headless=True, country="IN", on_event=on_event)
    assert saved == 6
    rows = {r["place_key"]: r for r in s.places(jid)}
    assert len(rows) == 6 and all(r["detail_status"] == "done" for r in rows.values())
    assert all(v == "pending" for v in stub_seen_pending.values()), "every place existed as a stub before its fill"
    assert rows["ChIJt1a"]["name"] == "biz ChIJt1a", "feed-card name survives the fill (COALESCE)"
    assert rows["ChIJt1a"]["rating"] == 4.5
    assert s.link_counts(jid) == (6, 6)
    assert s.get_job(jid)["status"] == "done"
    s.close()

    first_place = min(t for t, k, _ in timeline if k == "place")
    last_collect = max(t for t, k, _ in timeline if k == "collect")
    assert first_place < last_collect, f"opener never overlapped the collector: {timeline}"
    assert "links_done" in [k for _, k, _ in timeline]


def test_preset_links_runs_opener_only(db, monkeypatch, tmp_path):
    _fake_playwright(monkeypatch, tmp_path)
    jid = _job(db)

    def boom(*a, **k):
        raise AssertionError("collector must not run for preset links")

    monkeypatch.setattr(maps, "collect_place_links", boom)
    monkeypatch.setattr(maps, "scrape_place", lambda page, href, job_id, country: Place(
        job_id=job_id, place_key=href.split("!19s")[1], name="X", scraped_at=now_iso()))
    s = db()
    preset = [_card("ChIJpa"), _card("ChIJpb")]
    s.save_links(jid, preset)
    saved = maps.run_scrape(s, jid, "cafe", "Pune", 0, Pacing(delay_sec=0.001, pause_every=0),
                            headless=True, country="IN", preset_links=preset)
    assert saved == 2 and s.link_counts(jid) == (2, 2)
    assert s.count_places_detailed(jid) == 2
    s.close()


def test_stop_ends_opener_and_collector_promptly(db, monkeypatch, tmp_path):
    _fake_playwright(monkeypatch, tmp_path)
    jid = _job(db)
    n = {"tiles": 0}

    def slow_collect(page, want, on_progress=None):
        n["tiles"] += 1
        time.sleep(0.2)
        return [_card(f"ChIJs{n['tiles']}")]

    monkeypatch.setattr(maps, "collect_place_links", slow_collect)
    monkeypatch.setattr(maps, "scrape_place", lambda page, href, job_id, country: Place(
        job_id=job_id, place_key=href.split("!19s")[1], name="X", scraped_at=now_iso()))
    opened = {"n": 0}

    def should_stop():
        return opened["n"] >= 1

    def on_event(kind, data):
        if kind == "place":
            opened["n"] += 1

    s = db()
    t0 = time.monotonic()
    saved = maps.run_scrape(s, jid, ", ".join(f"kw{i}" for i in range(30)), "Pune", 0,
                            Pacing(delay_sec=0.001, pause_every=0), headless=True, country="IN",
                            on_event=on_event, should_stop=should_stop)
    took = time.monotonic() - t0
    assert saved == 1 and s.get_job(jid)["status"] == "stopped"
    assert took < 5, f"stop did not end the collector promptly ({took:.1f}s, {n['tiles']} tiles)"
    assert n["tiles"] < 30, "collector kept tiling after the stop"
    s.close()
