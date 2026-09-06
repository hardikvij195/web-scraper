"""T397 — orphan Chrome eviction, blank-tab pruning, restore-bubble suppression.

Pure-logic tests with fakes: no Playwright, no Chrome. The one behaviour that needs a real
process (SingletonLock symlink → PID) is exercised with our own PID and a dead one.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from webscraper import browser_recovery as br


class FakePage:
    def __init__(self, url: str) -> None:
        self.url = url
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeCtx:
    def __init__(self, pages: list[FakePage]) -> None:
        self.pages = pages


def test_close_blank_pages_keeps_the_working_page_and_real_tabs():
    keep = FakePage("about:blank")            # the page we are about to navigate
    blanks = [FakePage("about:blank"), FakePage(""), FakePage("about:blank")]
    real = FakePage("https://example.com/")
    ctx = FakeCtx([keep, *blanks, real])
    assert br.close_blank_pages(ctx, keep=keep) == 3
    assert not keep.closed and not real.closed
    assert all(p.closed for p in blanks)


def test_close_blank_pages_survives_a_dead_context():
    class Dead:
        @property
        def pages(self):
            raise RuntimeError("Target page, context or browser has been closed")
    assert br.close_blank_pages(Dead()) == 0


@pytest.mark.parametrize("msg", [
    "BrowserType.launch_persistent_context: Opening in existing browser session. This usually means that the profile is already in use by another instance of Chromium.",
    "Failed to create a ProcessSingleton for your profile directory.",
    "profile directory is in use",
])
def test_is_profile_busy_matches_chromes_wording(msg):
    assert br.is_profile_busy(msg)
    assert br.is_profile_busy(RuntimeError(msg))


def test_is_profile_busy_ignores_ordinary_failures():
    assert not br.is_profile_busy("Timeout 30000ms exceeded")
    assert not br.is_profile_busy("Target page, context or browser has been closed")


def test_mark_profile_clean_writes_exit_state(tmp_path: Path):
    prof = tmp_path / "fetch-profile"
    (prof / "Default").mkdir(parents=True)
    (prof / "Default" / "Preferences").write_text(json.dumps(
        {"profile": {"exit_type": "Crashed", "name": "Person 1"}, "other": 1}), encoding="utf-8")
    br.mark_profile_clean(prof)
    prefs = json.loads((prof / "Default" / "Preferences").read_text(encoding="utf-8"))
    assert prefs["profile"]["exit_type"] == "Normal"
    assert prefs["profile"]["exited_cleanly"] is True
    assert prefs["profile"]["name"] == "Person 1"                  # untouched
    assert prefs["session"]["restore_on_startup"] == 5
    assert prefs["other"] == 1


def test_mark_profile_clean_creates_a_missing_profile(tmp_path: Path):
    prof = tmp_path / "new-profile"
    br.mark_profile_clean(prof)
    assert json.loads((prof / "Default" / "Preferences").read_text())["profile"]["exit_type"] == "Normal"


def test_restore_bubble_args_carry_the_live_switch():
    assert "--hide-crash-restore-bubble" in br.RESTORE_BUBBLE_ARGS
    assert "--no-first-run" in br.RESTORE_BUBBLE_ARGS


@pytest.mark.skipif(sys.platform == "win32", reason="SingletonLock is a symlink on mac/linux only")
def test_stale_lock_is_cleared_but_a_live_holder_is_read(tmp_path: Path):
    prof = tmp_path / "p"
    prof.mkdir()
    os.symlink(f"host-{os.getpid()}", prof / "SingletonLock")
    assert br.lock_holder_pid(prof) == os.getpid()
    # our own pid is alive and NOT an orphan → the reaper must leave it alone
    assert br.reap_orphan_browsers([prof]) == 0
    assert (prof / "SingletonLock").is_symlink()
    # a dead pid → lock cleared, nothing killed
    dead = tmp_path / "d"
    dead.mkdir()
    os.symlink("host-999999999", dead / "SingletonLock")
    assert br.reap_orphan_browsers([dead]) == 0
    assert not (dead / "SingletonLock").exists()


def test_lock_holder_pid_none_without_lock(tmp_path: Path):
    assert br.lock_holder_pid(tmp_path) is None
    assert br.reap_orphan_browsers([tmp_path, tmp_path / "missing"]) == 0


def test_relauncher_evicts_a_busy_profile_then_retries(monkeypatch, tmp_path: Path):
    calls = {"open": 0, "killed": []}

    def open_fn():
        calls["open"] += 1
        if calls["open"] == 1:
            raise RuntimeError("Opening in existing browser session. This usually means that the profile is already in use")
        page = FakePage("about:blank")
        return FakeCtx([page, FakePage("about:blank")]), page

    monkeypatch.setattr(br, "kill_profile_holder", lambda d, reason="": calls["killed"].append((Path(d), reason)) or True)
    monkeypatch.setattr(br, "RELAUNCH_SETTLE_SEC", 0)
    rl = br.Relauncher(open_fn, profile_dir=tmp_path / "prof")
    ctx, page = rl.open()
    assert calls["open"] == 2
    assert calls["killed"] and calls["killed"][0][0] == tmp_path / "prof"
    # the extra blank tab the hand-off left behind is pruned on open
    assert ctx.pages[1].closed and not page.closed


def test_relauncher_without_profile_dir_still_raises_busy(monkeypatch):
    def open_fn():
        raise RuntimeError("Opening in existing browser session")
    with pytest.raises(RuntimeError):
        br.Relauncher(open_fn).open()


def test_relauncher_recover_evicts_the_zombie_before_relaunch(monkeypatch, tmp_path: Path):
    order: list[str] = []

    def open_fn():
        order.append("open")
        page = FakePage("about:blank")
        return FakeCtx([page]), page

    monkeypatch.setattr(br, "kill_profile_holder", lambda d, reason="": order.append("kill") or False)
    monkeypatch.setattr(br, "RELAUNCH_SETTLE_SEC", 0)
    rl = br.Relauncher(open_fn, profile_dir=tmp_path)
    rl.open()
    assert rl.recover("fetch x") is True
    assert order == ["open", "kill", "open"]
