"""W159 (CRM T1047): a lane-only job (nothing else running) on a machine with no WhatsApp session ends
`wa_no_session` right after the first failed re-probe instead of waiting WA_NO_SESSION_GIVE_UP_SEC —
DELL held #81 / #125 / #213 "running" for 15 min each with five more queued behind them."""
from __future__ import annotations

from webscraper import enrich as E, lanes as L
from tests.test_w144_lane_caps import _Clock, _relink_lane


def _setup(monkeypatch, lane_only: bool, rule_on: bool = True):
    L.reset_stage_gates()
    monkeypatch.setattr(L, "WA_RELINK_POLL_SEC", 0.001)
    monkeypatch.setenv("WA_NO_SESSION_GIVE_UP_SEC", "900")
    monkeypatch.setenv("WA_RELINK_REPROBE_SEC", "300")
    if rule_on:
        monkeypatch.delenv("LANE_ONE_JOB_PER_LANE", raising=False)
    else:
        monkeypatch.setenv("LANE_ONE_JOB_PER_LANE", "0")
    monkeypatch.setattr(L, "_relink_now", _Clock(step=50.0))
    monkeypatch.setattr(E, "browser_fallback_allowed", lambda: True)
    probes: list[str] = []
    monkeypatch.setattr(L, "_account_status", lambda name: probes.append(name) or "logged_out")
    lane, store = _relink_lane()
    store.flagged_wa_accounts = lambda: ["main"]
    store.wa_relinked_since = lambda since: False
    lane.ctl.other_lanes_done = lambda l: lane_only
    return lane, store, probes


def test_lane_only_job_hands_back_at_once(monkeypatch):
    lane, store, probes = _setup(monkeypatch, lane_only=True)
    out = L._wait_for_relink(lane, store, RuntimeError("WhatsApp account(s) need a fresh QR relink: main"))
    assert out == L.R_WA_NO_SESSION
    assert probes == ["main"], "exactly one probe, then hand back"
    assert any("W159" in n and "handing the WhatsApp pass back" in n for n in lane.notes), lane.notes


def test_job_with_other_lanes_still_running_waits_the_window(monkeypatch):
    lane, store, probes = _setup(monkeypatch, lane_only=False)
    out = L._wait_for_relink(lane, store, RuntimeError("x"))
    assert out == L.R_WA_NO_SESSION and len(probes) >= 3, probes      # the W144 window, re-probing


def test_rule_off_keeps_the_old_wait(monkeypatch):
    lane, store, probes = _setup(monkeypatch, lane_only=True, rule_on=False)
    out = L._wait_for_relink(lane, store, RuntimeError("x"))
    assert out == L.R_WA_NO_SESSION and len(probes) >= 3


def test_zero_give_up_still_means_wait_forever_semantics(monkeypatch):
    """give_up == 0 (W144 'wait for ever') must not be short-circuited by W159."""
    lane, store, probes = _setup(monkeypatch, lane_only=True)
    monkeypatch.setenv("WA_NO_SESSION_GIVE_UP_SEC", "0")
    import threading
    out: dict = {}
    t = threading.Thread(target=lambda: out.setdefault("r", L._wait_for_relink(lane, store, RuntimeError("x"))), daemon=True)
    t.start(); t.join(timeout=0.3)
    assert t.is_alive() and "r" not in out
    lane._stop = True if hasattr(lane, "_stop") else None
    lane.stopped = lambda: True
    t.join(timeout=2)
