"""W103 (CRM T546): one place whose navigation fails must not end the discovery lane.

`1 - PC` job #1625 (local 54): 7h18m in, 1455/1578 places opened, `page.goto` on one place
raised `net::ERR_ABORTED`. The opener's `except PWError` only knew "browser died → relaunch"
and re-raised everything else, so that single place ended the lane (`lane discovery failed`)
and 133 places were never opened. The WhatsApp lane has retried plain navigation failures
in place since T338; the opener now does the same.

Fakes only: no Playwright, no Chrome, no `data/leads.db`. `scrape_place` is the unit that
calls `page.goto`, so a stand-in that raises the same `PWError` exercises the same handler.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
from playwright.sync_api import Error as PWError

from webscraper import browser_recovery as br
from webscraper import maps
from webscraper.maps import FeedCard, Pacing
from webscraper.models import Place
from webscraper.store import Store, now_iso

ABORTED = ("Page.goto: net::ERR_ABORTED at https://www.google.com/maps/place/Lifespan+Mortgage+Services/"
           "data=!4m7!3m6!1s0x2a32a3041b47879f:0x8c76ddc7b967e224\nCall log:\n  - navigating to ...")
RESET = "Page.goto: net::ERR_CONNECTION_RESET at https://www.google.com/maps/place/x"
CLOSED = "Page.goto: Target page, context or browser has been closed"


# ── fakes ─────────────────────────────────────────────────────────────────────────────
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


class _Scraper:
    """`scrape_place` stand-in. `failures[key]` = messages to raise, in order, before that
    place finally opens; a key with an empty list opens first time."""

    def __init__(self, failures: dict[str, list[str]] | None = None) -> None:
        self.failures = {k: list(v) for k, v in (failures or {}).items()}
        self.calls: list[str] = []

    def __call__(self, page, href, job_id, country) -> Place:
        key = href.split("!19s")[1]
        self.calls.append(key)
        pending = self.failures.get(key) or []
        if pending:
            raise PWError(pending.pop(0))
        return Place(job_id=job_id, place_key=key, name=f"biz {key}", scraped_at=now_iso())


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "w103.db"
    return lambda: Store(path)


def _setup(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(maps, "sync_playwright", lambda: _FakePW())
    monkeypatch.setattr(maps.settings, "profile_dir", tmp_path / "browser-profile")
    monkeypatch.setattr(maps.random, "uniform", lambda a, b: 0.001)
    monkeypatch.setattr(maps, "OPENER_POLL_SEC", 0.05)
    monkeypatch.setattr(maps, "NAV_RETRY_SLEEP_SEC", 0.0, raising=False)
    monkeypatch.setattr(br, "RELAUNCH_SETTLE_SEC", 0.0)
    monkeypatch.setattr(br, "kill_profile_holder", lambda *a, **k: 0)


def _card(key: str) -> FeedCard:
    return FeedCard(href=f"https://www.google.com/maps/place/x/data=!19s{key}", name=f"biz {key}",
                    rating=4.5, reviews_count=120, lat=18.5, lng=73.8)


def _run(db, preset: list[FeedCard], scraper: _Scraper, monkeypatch, events: list) -> tuple[Store, int]:
    monkeypatch.setattr(maps, "scrape_place", scraper)
    s = db()
    jid = s.create_job(query="cafe", location="Pune", max_places=0, delay_sec=0)
    saved = maps.run_scrape(s, jid, "cafe", "Pune", 0, Pacing(delay_sec=0.001, pause_every=0),
                            headless=True, country="IN", preset_links=preset,
                            on_event=lambda k, d: events.append((k, d)))
    return s, jid, saved


def _skips(events: list) -> list[str]:
    return [d["reason"] for k, d in events if k == "skip"]


# ── tests ─────────────────────────────────────────────────────────────────────────────
def test_nav_failure_once_is_retried_in_place_and_the_place_opens(db, monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    scraper = _Scraper({"ChIJa": [ABORTED]})
    events: list = []
    s, jid, saved = _run(db, [_card("ChIJa"), _card("ChIJb")], scraper, monkeypatch, events)
    assert saved == 2
    assert scraper.calls == ["ChIJa", "ChIJa", "ChIJb"]           # retried once, then moved on
    assert s.count_places_detailed(jid) == 2
    assert _skips(events) == []
    assert [k for k, _ in events if k == "browser_restart"] == []   # alive browser: no relaunch
    s.close()


def test_nav_failure_twice_skips_that_place_and_the_lane_continues(db, monkeypatch, tmp_path, caplog):
    _setup(monkeypatch, tmp_path)
    scraper = _Scraper({"ChIJa": [ABORTED, RESET]})
    events: list = []
    with caplog.at_level(logging.WARNING, logger="webscraper.maps"):
        s, jid, saved = _run(db, [_card("ChIJa"), _card("ChIJb")], scraper, monkeypatch, events)
    assert saved == 1                                             # the lane did not die
    assert scraper.calls == ["ChIJa", "ChIJa", "ChIJb"]
    assert _skips(events) == ["navigation failed"]
    assert any("skipped biz ChIJa" in r.getMessage() and "ERR_CONNECTION_RESET" in r.getMessage()
               for r in caplog.records)
    # Opened = attempted, so the link is spent; the stub is still `pending`, which is exactly
    # what W62/W98's "re-open the stubs" hands back to the opener on the next pending run.
    assert s.link_counts(jid) == (2, 2)
    assert s.count_places_detailed(jid) == 1
    assert s.reopen_stub_links(jid) == 1
    assert s.next_pending_link(jid)["key"] == "ChIJa"
    assert s.get_job(jid)["status"] == "done"
    s.close()


def test_dead_browser_still_takes_the_relaunch_path(db, monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    scraper = _Scraper({"ChIJa": [CLOSED]})
    events: list = []
    s, jid, saved = _run(db, [_card("ChIJa"), _card("ChIJb")], scraper, monkeypatch, events)
    assert saved == 1
    assert scraper.calls == ["ChIJa", "ChIJb"]                    # a relaunch costs that one place
    assert [d["attempt"] for k, d in events if k == "browser_restart"] == [1]
    assert _skips(events) == ["browser restarted"]
    s.close()


def test_five_consecutive_skipped_places_fail_the_lane_with_the_last_error(db, monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    keys = [f"ChIJ{i}" for i in range(6)]
    scraper = _Scraper({k: [ABORTED, RESET] for k in keys})
    events: list = []
    with pytest.raises(PWError, match="ERR_CONNECTION_RESET"):
        _run(db, [_card(k) for k in keys], scraper, monkeypatch, events)
    assert len(scraper.calls) == 5 * 2                            # five places, two tries each; the sixth never
    assert _skips(events) == ["navigation failed"] * 5


def test_a_successful_place_resets_the_consecutive_failure_count(db, monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    bad = [f"ChIJb{i}" for i in range(4)]
    bad2 = [f"ChIJc{i}" for i in range(4)]
    failures = {k: [ABORTED, ABORTED] for k in bad + bad2}
    scraper = _Scraper(failures)
    events: list = []
    preset = [_card(k) for k in bad] + [_card("ChIJok")] + [_card(k) for k in bad2]
    s, jid, saved = _run(db, preset, scraper, monkeypatch, events)
    assert saved == 1
    assert _skips(events) == ["navigation failed"] * 8            # 4 + 4, never 5 in a row
    assert s.get_job(jid)["status"] == "done"
    s.close()
