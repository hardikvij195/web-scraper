"""W120 (CRM T767): planned browser recycling — a headed Maps tab (or the enrichment
fallback Chrome) grows past 1 GB over a long run if it is never relaunched. Fakes only:
no Playwright, no Chrome.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from webscraper import browser_fetch as bf
from webscraper import browser_recovery as br
from webscraper import maps


# ── recycle_due ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("count, every, expected", [
    (1, 40, False),
    (41, 40, True),
    (42, 40, False),
    (81, 40, True),
    (5, 0, False),
])
def test_recycle_due(count: int, every: int, expected: bool) -> None:
    assert maps.recycle_due(count, every) is expected


# ── Relauncher.recycle ───────────────────────────────────────────────────────────
class _Ctx:
    def __init__(self) -> None:
        self.closed = False
        self.pages: list = []

    def close(self) -> None:
        self.closed = True


def test_relauncher_recycle_closes_old_opens_new_and_skips_on_restart(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(br, "RELAUNCH_SETTLE_SEC", 0)
    restarts: list[tuple[str, int]] = []
    opened: list[_Ctx] = []

    def open_fn():
        c = _Ctx()
        opened.append(c)
        return c, object()

    rl = br.Relauncher(open_fn, on_restart=lambda where, n: restarts.append((where, n)),
                        profile_dir=tmp_path)
    rl.open()
    first = rl.ctx
    ctx2, _page2 = rl.recycle("place 41")
    assert first.closed is True
    assert ctx2 is opened[-1] and ctx2 is not first
    assert rl.attempts == 0
    assert restarts == []


# ── BrowserFetcher idle close ────────────────────────────────────────────────────
class _FakePW:
    made: list["_FakePW"] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeCtx:
    pages: list = []

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_browser_fetch_closes_idle_and_reopens_on_next_fetch(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(bf, "IDLE_CLOSE_SEC", 0.05)
    monkeypatch.setattr(bf, "reap_orphan_browsers", lambda dirs, reason="": 0)
    opens: list[_FakeCtx] = []

    class Fetcher(bf.BrowserFetcher):
        def _playwright(self):
            return _FakePW()

        def _profile_dir(self):
            return tmp_path / "prof"

        def _opener(self, pw):
            def _open():
                c = _FakeCtx()
                opens.append(c)
                return c, object()
            return _open

        def _goto(self, rl, url):
            return "<html>ok</html>", None

    fetcher = Fetcher(headless=True)
    assert len(opens) == 1

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not opens[0].closed:
        time.sleep(0.02)
    assert opens[0].closed, "idle Chrome was never closed"

    html, err = fetcher.fetch_ex("https://example.com/")
    assert (html, err) == ("<html>ok</html>", None)
    assert len(opens) == 2, "fetch after idle close should reopen the browser"

    fetcher.close()
