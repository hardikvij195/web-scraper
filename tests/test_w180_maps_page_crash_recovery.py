"""W180 (2 - MAC, 2026-10-07): the Maps lane survives a Chrome renderer crash.

Between 16:49 and 18:11 IST the Mac's opener raised `Page.goto: Page crashed` 20 times (RAM
69-83 %). `is_closed()` is false for it (the context is alive), so the W103 path treated it as
a navigation error: retry on the SAME dead page (crashes again), skip the place, and five such
places in a row ended the lane — `lane failed: Page.goto: Page crashed` at 341/729 (#22571) and
175/546 (#22574). A crashed page can never be reused; the opener now replaces the tab, retries
the same place once, relaunches the context after `MAPS_CRASH_RELAUNCH_AFTER` crashes and only
fails the lane after `MAPS_CRASH_MAX` crashes in a row.

W181: `CHROME_LEAN_ARGS` / `MAPS_BLOCK_ASSETS` resolve `__<DEVICE>` first, like `MAPS_RELAUNCH`.

Fakes only: no Playwright, no Chrome, no `data/leads.db`. The `scrape_place` stand-in calls
`page.goto`, so the crash comes out of the page the opener holds — the thing being replaced.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
from playwright.sync_api import Error as PWError

from webscraper import browser_recovery as br
from webscraper import chrome_args, maps
from webscraper.maps import FeedCard, Pacing
from webscraper.models import Place
from webscraper.store import Store, now_iso

CRASH = ("Page.goto: Page crashed\nCall log:\n  - navigating to \"https://www.google.com/maps/place/x\", "
         "waiting until \"domcontentloaded\"")


# ── fakes ─────────────────────────────────────────────────────────────────────────────
class _FakePage:
    """`crashes` > 0 = this tab crashes on its first `goto`; 0 = it works; -N = it works for N
    `goto` calls and then crashes. A crashed tab stays dead: every later `goto` crashes too
    (exactly what Chrome does), so the opener MUST replace it to make progress."""
    url = "https://www.google.com/maps/search/x"

    def __init__(self, crashes: int = 0) -> None:
        self.works = -crashes if crashes < 0 else 0
        self.crashes = 1 if crashes < 0 else crashes
        self.dead = False
        self.closed = False

    def set_default_timeout(self, *a): pass

    def goto(self, *a, **k):
        if self.works > 0 and not self.dead:
            self.works -= 1
            return
        if self.dead or self.crashes > 0:
            self.crashes -= 1
            self.dead = True
            raise PWError(CRASH)

    def close(self):
        self.closed = True


class _FakeCtx:
    """`budgets` = crash counts handed to each page in turn (first = `pages[0]`); the list is
    SHARED across relaunched contexts so a test scripts the whole lane in one sequence."""

    def __init__(self, budgets: list[int]) -> None:
        self.budgets = budgets
        self.pages = [_FakePage(self._next())]
        self.new_pages = 0

    def _next(self) -> int:
        return self.budgets.pop(0) if self.budgets else 0

    def new_page(self):
        self.new_pages += 1
        pg = _FakePage(self._next())
        self.pages.append(pg)
        return pg

    def route(self, *a, **k): pass

    def close(self): pass


class _FakePW:
    """Every `launch_persistent_context` (first open + each W180/W120 relaunch) is counted."""
    launches = 0
    budgets: list[int] = []

    class chromium:
        @staticmethod
        def launch_persistent_context(**kw):
            _FakePW.launches += 1
            return _FakeCtx(_FakePW.budgets)

    def __enter__(self): return self

    def __exit__(self, *a): pass


class _Scraper:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.pages: list[_FakePage] = []

    def __call__(self, page, href, job_id, country) -> Place:
        key = href.split("!19s")[1]
        self.calls.append(key)
        self.pages.append(page)
        page.goto(href)                                   # the crash comes from the opener's own tab
        return Place(job_id=job_id, place_key=key, name=f"biz {key}", scraped_at=now_iso())


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "w180.db"
    return lambda: Store(path)


def _setup(monkeypatch, tmp_path: Path, budgets: list[int]) -> None:
    _FakePW.launches = 0
    _FakePW.budgets = budgets                            # shared across relaunched contexts
    monkeypatch.setattr(maps, "sync_playwright", lambda: _FakePW())
    monkeypatch.setattr(maps.settings, "profile_dir", tmp_path / "browser-profile")
    monkeypatch.setattr(maps.random, "uniform", lambda a, b: 0.001)
    monkeypatch.setattr(maps, "OPENER_POLL_SEC", 0.05)
    monkeypatch.setattr(maps, "NAV_RETRY_SLEEP_SEC", 0.0, raising=False)
    monkeypatch.setattr(br, "RELAUNCH_SETTLE_SEC", 0.0)
    monkeypatch.setattr(br, "kill_profile_holder", lambda *a, **k: 0)
    monkeypatch.delenv("MAPS_CRASH_MAX", raising=False)
    monkeypatch.delenv("MAPS_CRASH_RELAUNCH_AFTER", raising=False)


def _card(key: str) -> FeedCard:
    return FeedCard(href=f"https://www.google.com/maps/place/x/data=!19s{key}", name=f"biz {key}",
                    rating=4.5, reviews_count=120, lat=18.5, lng=73.8)


def _run(db, preset: list[FeedCard], scraper: _Scraper, monkeypatch, events: list):
    monkeypatch.setattr(maps, "scrape_place", scraper)
    s = db()
    jid = s.create_job(query="cafe", location="Pune", max_places=0, delay_sec=0)
    saved = maps.run_scrape(s, jid, "cafe", "Pune", 0, Pacing(delay_sec=0.001, pause_every=0),
                            headless=True, country="IN", preset_links=preset,
                            on_event=lambda k, d: events.append((k, d)))
    return s, jid, saved


def _skips(events: list) -> list[str]:
    return [d["reason"] for k, d in events if k == "skip"]


# ── W180 ──────────────────────────────────────────────────────────────────────────────
def test_is_page_crashed_is_not_is_closed():
    e = PWError(CRASH)
    assert maps.is_page_crashed(e)
    assert not br.is_closed(e)                            # why W103 caught it before this fix
    assert not maps.is_page_crashed(PWError("Page.goto: net::ERR_ABORTED at https://x"))
    assert maps.maps_crash_max() == 5 and maps.maps_crash_relaunch_after() == 3


def test_one_crash_reopens_the_tab_and_the_same_place_is_processed_not_skipped(db, monkeypatch, tmp_path, caplog):
    _setup(monkeypatch, tmp_path, budgets=[1])            # tab 1 crashes once; tab 2 is fine
    scraper = _Scraper()
    events: list = []
    with caplog.at_level(logging.INFO, logger="webscraper.maps"):
        s, jid, saved = _run(db, [_card("ChIJa"), _card("ChIJb")], scraper, monkeypatch, events)
    assert saved == 2                                     # the crashed place was processed
    assert scraper.calls == ["ChIJa", "ChIJa", "ChIJb"]   # retried once, on the NEW tab
    assert scraper.pages[0] is not scraper.pages[1] and scraper.pages[0].closed
    assert scraper.pages[1] is scraper.pages[2]
    assert _skips(events) == []                           # never `skipped` on the first crash
    assert [k for k, _ in events if k == "browser_restart"] == []
    assert _FakePW.launches == 1                          # a tab, not a whole context
    assert [d["count"] for k, d in events if k == "page_crashed"] == [1]
    assert any("Maps page crashed — reopened the tab and continuing (W180, crash 1 this lane" in r.getMessage()
               for r in caplog.records)
    assert [d for k, d in events if k == "page_crashes_recovered"] == [{"count": 1}]
    assert any("1 page crashes recovered" in r.getMessage() for r in caplog.records)
    assert s.get_job(jid)["status"] == "done"
    s.close()


def test_retry_crash_skips_with_the_w103_text_and_the_lane_goes_on(db, monkeypatch, tmp_path, caplog):
    _setup(monkeypatch, tmp_path, budgets=[1, 1])         # tab 1 and its replacement both crash
    scraper = _Scraper()
    events: list = []
    with caplog.at_level(logging.WARNING, logger="webscraper.maps"):
        s, jid, saved = _run(db, [_card("ChIJa"), _card("ChIJb")], scraper, monkeypatch, events)
    assert saved == 1
    assert scraper.calls == ["ChIJa", "ChIJa", "ChIJb"]
    assert _skips(events) == ["navigation failed"]
    assert any("skipped biz ChIJa" in r.getMessage() and "Page crashed" in r.getMessage()
               for r in caplog.records)
    assert s.get_job(jid)["status"] == "done"
    s.close()


def test_crashes_reaching_relaunch_after_recycle_the_whole_context(db, monkeypatch, tmp_path):
    # context 1: tab 1 crashes once (-> new tab), that tab crashes once (2nd crash -> relaunch);
    # context 2: its first tab is fine.
    _setup(monkeypatch, tmp_path, budgets=[1, 1, 0])
    monkeypatch.setenv("MAPS_CRASH_RELAUNCH_AFTER", "2")
    scraper = _Scraper()
    events: list = []
    s, jid, saved = _run(db, [_card("ChIJa"), _card("ChIJb")], scraper, monkeypatch, events)
    assert saved == 1 and scraper.calls == ["ChIJa", "ChIJa", "ChIJb"]
    assert _FakePW.launches == 2                          # the W120 recycle path, not a crash relaunch
    assert [d["relaunched"] for k, d in events if k == "page_crashed"] == [False, True]
    assert [k for k, _ in events if k == "browser_restart"] == []
    s.close()


def test_maps_crash_max_consecutive_crashes_fail_the_lane_with_todays_message(db, monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, budgets=[99] * 20)      # every tab, in every context, crashes
    scraper = _Scraper()
    events: list = []
    keys = [f"ChIJ{i}" for i in range(6)]
    with pytest.raises(PWError, match="Page crashed"):
        _run(db, [_card(k) for k in keys], scraper, monkeypatch, events)
    # a: crash, retry-crash -> skipped; b: same; c: 5th crash in a row -> lane fails
    assert len([k for k, _ in events if k == "page_crashed"]) == 4
    assert _skips(events) == ["navigation failed"] * 2
    assert scraper.calls == ["ChIJ0", "ChIJ0", "ChIJ1", "ChIJ1", "ChIJ2"]


def test_a_place_that_opens_resets_the_crash_streak(db, monkeypatch, tmp_path):
    # tab 1 crashes on place 0; every replacement opens ONE place and crashes on the next ->
    # 7 crashes in the lane, each followed by a place that opened, so never 5 in a row.
    _setup(monkeypatch, tmp_path, budgets=[1, -1, -1, -1, -1, -1, -1, 0])
    monkeypatch.setenv("MAPS_CRASH_RELAUNCH_AFTER", "100")
    scraper = _Scraper()
    events: list = []
    keys = [f"ChIJ{i}" for i in range(7)]
    s, jid, saved = _run(db, [_card(k) for k in keys], scraper, monkeypatch, events)
    assert saved == 7 and _skips(events) == []
    assert [d for k, d in events if k == "page_crashes_recovered"] == [{"count": 7}]
    assert _FakePW.launches == 1
    assert s.get_job(jid)["status"] == "done"
    s.close()


# ── W181 ──────────────────────────────────────────────────────────────────────────────
def test_w181_per_device_flag_wins_over_generic(monkeypatch):
    monkeypatch.setattr(chrome_args, "device_upper", lambda: "2 - MAC")
    monkeypatch.setenv("CHROME_LEAN_ARGS", "1")
    monkeypatch.setenv("CHROME_LEAN_ARGS__2 - MAC", "0")
    monkeypatch.setenv("MAPS_BLOCK_ASSETS", "1")
    monkeypatch.setenv("MAPS_BLOCK_ASSETS__2 - MAC", "0")
    assert chrome_args.enabled() is False and chrome_args.lean_args("maps") == []
    assert maps.block_assets_enabled() is False


def test_w181_generic_used_without_a_per_device_key_and_default_is_on(monkeypatch):
    monkeypatch.setattr(chrome_args, "device_upper", lambda: "4 - DELL")
    monkeypatch.setenv("CHROME_LEAN_ARGS__2 - MAC", "0")           # another machine's switch
    monkeypatch.setenv("MAPS_BLOCK_ASSETS__2 - MAC", "0")
    monkeypatch.setenv("CHROME_LEAN_ARGS", "0")
    monkeypatch.delenv("MAPS_BLOCK_ASSETS", raising=False)
    assert chrome_args.enabled() is False                          # generic off
    assert maps.block_assets_enabled() is True                     # default on
    monkeypatch.delenv("CHROME_LEAN_ARGS", raising=False)
    assert chrome_args.enabled() is True
