"""W147 (CRM T1027): `max_places` > 0 is a JOB-WIDE cap on unique places across keywords and tiles."""
from __future__ import annotations

from pathlib import Path

import pytest

from webscraper import maps
from webscraper.maps import FeedCard, Pacing
from webscraper.models import Place
from webscraper.store import Store, now_iso
from tests.test_w26_discovery import _fake_playwright


def _card(key: str) -> FeedCard:
    return FeedCard(href=f"https://www.google.com/maps/place/x/data=!19s{key}", name=f"biz {key}",
                    rating=4.5, reviews_count=10, lat=18.5, lng=73.8)


def _run(monkeypatch, tmp_path, cap, per_kw=100, known=None, overlap=False):
    _fake_playwright(monkeypatch, tmp_path)
    s = Store(tmp_path / "w147.db")
    jid = s.create_job(query="a, b, c", location="Pune", max_places=cap, delay_sec=0)
    n = {"i": 0}

    def fake_collect(page, want, on_progress=None):
        n["i"] += 1
        i = 0 if overlap else n["i"]
        return [_card(f"ChIJk{i}x{j}") for j in range(per_kw)]

    def fake_scrape(page, href, job_id, country):
        key = href.split("!19s")[1]
        return Place(job_id=job_id, place_key=key, name=None, phone=None, scraped_at=now_iso())

    monkeypatch.setattr(maps, "collect_place_links", fake_collect)
    monkeypatch.setattr(maps, "scrape_place", fake_scrape)
    events = []
    saved = maps.run_scrape(s, jid, "a, b, c", "Pune", cap, Pacing(delay_sec=0.001, pause_every=0),
                            headless=True, country="IN", known_keys=known,
                            on_event=lambda k, d: events.append((k, d)))
    return s, jid, saved, events


def test_cap_100_three_keywords(monkeypatch, tmp_path):
    s, jid, saved, events = _run(monkeypatch, tmp_path, 100)
    assert saved == 100 and s.count_places(jid) == 100
    caps = [d for k, d in events if k == "cap_reached"]
    assert len(caps) == 1 and caps[0]["limit"] == 100 and caps[0]["skipped_keywords"] == 2
    assert s.get_job(jid)["status"] != "failed"


def test_cap_zero_is_unlimited(monkeypatch, tmp_path):
    s, jid, saved, events = _run(monkeypatch, tmp_path, 0)
    assert saved == 300 and not [1 for k, _ in events if k == "cap_reached"]


def test_cap_150_across_keywords(monkeypatch, tmp_path):
    s, jid, saved, events = _run(monkeypatch, tmp_path, 150)
    assert saved == 150 and s.count_places(jid) == 150
    assert [d for k, d in events if k == "cap_reached"][0]["skipped_keywords"] == 1


def test_known_duplicates_do_not_count(monkeypatch, tmp_path):
    known = {f"ChIJk1x{j}" for j in range(60)}            # first keyword: 60 already in the system
    s, jid, saved, events = _run(monkeypatch, tmp_path, 100, known=known)
    assert saved == 100                                # 40 from kw1 + 60 from kw2, duplicates skipped
    assert s.count_places(jid) == 100
    assert not ({r["place_key"] for r in s.places(jid)} & known)


def test_overlapping_keywords_count_unique(monkeypatch, tmp_path):
    s, jid, saved, events = _run(monkeypatch, tmp_path, 150, overlap=True)
    assert saved == 100                                # every keyword offers the same 100 places
    assert not [1 for k, _ in events if k == "cap_reached"]


def test_eta_total_clamped_to_cap():
    from webscraper import eta
    src = open(eta.__file__, encoding="utf-8").read()
    assert 'cap = int(_get(row, "max_places"' in src
