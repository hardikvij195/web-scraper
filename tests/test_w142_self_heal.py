"""W142 (CRM T1020, the Mac 2026-10-05): the agent heals itself — a silent CRM loop restarts the
process, a WhatsApp profile held by a stale Chrome is evicted and the launch retried once, and a
WhatsApp command on a busy machine pauses the lane instead of refusing."""
from __future__ import annotations

from pathlib import Path

import pytest

from webscraper import browser_recovery as br
from webscraper import wa_verify
from webscraper.agent import _watchdog_should_restart as trip

LIMIT = 300.0
MAC_MSG = ("BrowserType.launch_persistent_context: Opening in existing browser session. This usually "
           "means that the profile is already in use")


# -- 1. loop watchdog (pure decision) -----------------------------------------------------

def test_watchdog_disabled_never_trips():
    assert trip(10_000, 0, 0, 0, 0) is False


def test_watchdog_fresh_heartbeat_does_not_trip():
    assert trip(1000, 900, 950, 0, LIMIT) is False


def test_watchdog_loop_dead_no_attempts_trips():
    # last attempt == last ok, nothing at all since: the loop is simply not running (the Mac)
    assert trip(1000, 500, 500, 0, LIMIT) is True


def test_watchdog_offline_machine_does_not_restart_loop():
    # erroring every 5 s for 25 min: the loop is alive, the network is not
    assert trip(2000, 500, 1998, 1999, LIMIT) is False


def test_watchdog_in_flight_attempt_while_offline_is_not_hung():
    assert trip(2000, 500, 1999, 1995, LIMIT) is False


def test_watchdog_hung_attempt_trips():
    # attempt started 400 s ago after a quick error, never came back
    assert trip(2000, 500, 1600, 1550, LIMIT) is True


def test_watchdog_errors_then_silence_trips():
    assert trip(2000, 500, 900, 905, LIMIT) is True


# -- 2. profile-lock eviction -------------------------------------------------------------

def test_mac_lock_message_is_profile_busy():
    assert br.is_profile_busy(MAC_MSG)
    assert br.is_profile_busy(RuntimeError("the profile is already in use"))
    assert not br.is_profile_busy("Target page, context or browser has been closed")


def test_profile_holder_pids_matches_exact_dir_only():
    d = Path("/Users/x/web-scraper/data/wa-profiles/main")
    procs = [
        (11, f"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir={d} --no-first-run"),
        (12, f'chrome.exe --user-data-dir="{d}" --type=renderer'),
        (13, f"chrome --user-data-dir={d}-2 --no-first-run"),          # another account
        (14, f"chrome --user-data-dir={d}/Default"),                    # not the profile root
        (15, "chrome --user-data-dir=/elsewhere/main"),
        (16, f"python -m webscraper agent {d}"),                        # no user-data-dir flag
        (17, f"chrome --user-data-dir={d}"),                            # at end of line
        (18, None),
    ]
    assert br.profile_holder_pids(d, procs) == [11, 12, 17]


def test_profile_holder_pids_windows_backslashes_case_insensitive():
    d = Path(r"C:\Users\h\web-scraper\data\wa-profiles\main")
    procs = [(5, r'"C:\Program Files\Google\Chrome\Application\chrome.exe" '
                 r'--user-data-dir=c:\users\h\web-scraper\data\wa-profiles\MAIN --lang=en')]
    assert br.profile_holder_pids(d, procs) == [5]


def test_launch_evicting_retries_once_after_evicting(tmp_path):
    calls, evicted, settled = [], [], []

    def open_fn():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError(MAC_MSG)
        return "ctx"

    out = br.launch_evicting(open_fn, tmp_path, "test",
                             evict=lambda d, why: evicted.append(d), settle=lambda d, s: settled.append(s))
    assert out == "ctx" and len(calls) == 2
    assert evicted == [tmp_path] and settled == [br.EVICT_WAIT_SEC]


def test_launch_evicting_other_errors_propagate_untouched(tmp_path):
    evicted = []

    def open_fn():
        raise RuntimeError("Target page, context or browser has been closed")

    with pytest.raises(RuntimeError):
        br.launch_evicting(open_fn, tmp_path, evict=lambda *a: evicted.append(1), settle=lambda *a: None)
    assert evicted == []


def test_launch_evicting_gives_up_after_one_retry(tmp_path):
    n = []

    def open_fn():
        n.append(1)
        raise RuntimeError(MAC_MSG)

    with pytest.raises(RuntimeError):
        br.launch_evicting(open_fn, tmp_path, evict=lambda *a: None, settle=lambda *a: None)
    assert len(n) == 2


# -- 3. WA commands pause the lane --------------------------------------------------------

class FakeLane:
    """Holds `name`'s Chrome until it notices the pause (after `release_after` polls)."""

    def __init__(self, release_after: int):
        self.polls = 0
        self.release_after = release_after
        self.seen_paused: list[bool] = []

    def in_use(self, name: str) -> bool:
        self.polls += 1
        self.seen_paused.append(wa_verify.account_paused(name))
        return self.polls <= self.release_after


def _clock():
    t = [0.0]

    def sleep(s: float) -> None:
        t[0] += s
    return t, sleep, (lambda: t[0])


def test_command_waits_for_the_lane_then_runs_and_resumes():
    lane, evicted, ran = FakeLane(release_after=2), [], []
    t, sleep, now = _clock()
    out = wa_verify.with_lane_paused(
        "main", lambda: ran.append(wa_verify.account_paused("main")) or "done",
        in_use=lane.in_use, evict=lambda n: evicted.append(n), sleep=sleep, now=now)
    assert out == "done"
    assert ran == [True]                       # the command ran while the lane was told to keep off
    assert lane.seen_paused and all(lane.seen_paused)
    assert evicted == []
    assert not wa_verify.account_paused("main")   # resumed afterwards
    assert t[0] == 4.0


def test_command_evicts_when_the_lane_holds_on_past_the_deadline():
    lane, evicted = FakeLane(release_after=10_000), []
    t, sleep, now = _clock()
    out = wa_verify.with_lane_paused("main", lambda: "done", wait_sec=90,
                                     in_use=lane.in_use, evict=lambda n: evicted.append(n), sleep=sleep, now=now)
    assert out == "done" and evicted == ["main"] and t[0] >= 90
    assert not wa_verify.account_paused("main")


def test_pause_lifts_even_when_the_command_raises():
    def boom():
        raise ValueError("x")
    with pytest.raises(ValueError):
        wa_verify.with_lane_paused("main", boom, in_use=lambda n: False, evict=lambda n: None,
                                   sleep=lambda s: None, now=lambda: 0.0)
    assert not wa_verify.account_paused("main")


def test_lane_waits_while_paused_and_resumes_when_lifted():
    wa_verify._PAUSED.add("main")
    t, sleep, now = _clock()

    def sleep_then_lift(s: float) -> None:
        sleep(s)
        if t[0] >= 6:
            wa_verify._PAUSED.discard("main")
    assert wa_verify._wait_while_paused(lambda: False, "main", sleep=sleep_then_lift, now=now) is True
    assert t[0] == 6.0


def test_lane_wait_gives_up_on_stop_or_timeout():
    wa_verify._PAUSED.add("spare1")
    try:
        t, sleep, now = _clock()
        assert wa_verify._wait_while_paused(lambda: True, None, sleep=sleep, now=now) is False
        assert wa_verify._wait_while_paused(lambda: False, "spare1", max_sec=10, sleep=sleep, now=now) is False
        assert t[0] >= 10
    finally:
        wa_verify._PAUSED.discard("spare1")


def test_busy_delete_runs_under_the_pause_instead_of_refusing(monkeypatch):
    seq = []
    monkeypatch.setattr(wa_verify, "with_lane_paused",
                        lambda name, fn, **kw: (seq.append(("pause", name)), fn(), seq.append(("resume", name)))[1])
    monkeypatch.setattr(wa_verify, "_log_out_of_whatsapp",
                        lambda name: (seq.append(("logout", name)), (True, "unlinked"))[1])
    monkeypatch.setattr(wa_verify, "reset_account",
                        lambda name, busy=False: (seq.append(("reset", name, busy)), (True, "reset"))[1])
    ok, msg = wa_verify.delete_account("main", busy=True)
    assert ok and "deleted main" in msg
    assert seq == [("pause", "main"), ("logout", "main"), ("reset", "main", False), ("resume", "main")]


def test_busy_login_runs_under_the_pause(monkeypatch):
    seq = []
    monkeypatch.setattr(wa_verify, "with_lane_paused", lambda name, fn, **kw: (seq.append(name), fn())[1])
    real = wa_verify.login
    # the inner call runs with busy=False — stub that half; the pause half is what is under test
    monkeypatch.setattr(wa_verify, "login", lambda name, busy=False: real(name, busy=busy) if busy else True)
    assert wa_verify.login("main", busy=True) is True
    assert seq == ["main"]
