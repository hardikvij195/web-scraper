"""W186 (CRM T1247-c): manual per-machine lane pause (`lane_pause__<device>` -> LANE_PAUSE__<DEVICE>)."""
from __future__ import annotations

import os
import threading
import time

import pytest

from webscraper import agent as A
from webscraper import lanes as L


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(A, "DEVICE_NAME", "3 - ASUS")
    for k in [k for k in os.environ if k.startswith("LANE_PAUSE")]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(L, "PAUSE_POLL_SEC", 0.01)
    L.PAUSED_NOW.clear()
    yield
    L.PAUSED_NOW.clear()


def test_parse_comma_list_case_and_space_tolerant():
    assert L.parse_lane_pause(" Maps , WhatsApp ", None) == {"discovery", "whatsapp"}
    assert L.parse_lane_pause("websites;wa", None) == {"enrichment", "whatsapp"}
    assert L.parse_lane_pause("", None) == frozenset()
    assert L.parse_lane_pause(None, None) == frozenset()
    assert L.parse_lane_pause("bogus, none", None) == frozenset()


def test_parse_per_device_wins_even_none():
    assert L.parse_lane_pause("none", "maps,websites") == frozenset()
    assert L.parse_lane_pause("websites", "maps") == {"enrichment"}
    assert L.parse_lane_pause("  ", "maps") == {"discovery"}


def test_paused_lanes_reads_device_env(monkeypatch):
    monkeypatch.setenv("LANE_PAUSE", "maps")
    assert L.paused_lanes() == {"discovery"}
    monkeypatch.setenv("LANE_PAUSE__3 - ASUS", "WhatsApp")
    assert L.lane_paused("whatsapp") and not L.lane_paused("discovery")


def _lane_with_gate(monkeypatch, job_id=1):
    gate = L.StageGate("whatsapp", 1)
    monkeypatch.setitem(L.STAGE_GATES, "whatsapp", gate)
    lane = L.WhatsAppLane(job_id, {}, None)
    assert gate.acquire(job_id, lambda: False, lambda m: None)
    return lane, gate


def _wait_for(cond, timeout=3.0):
    t0 = time.monotonic()
    while not cond():
        assert time.monotonic() - t0 < timeout, "timed out"
        time.sleep(0.01)


def test_paused_lane_gives_its_slot_back_and_resumes(monkeypatch):
    lane, gate = _lane_with_gate(monkeypatch)
    monkeypatch.setenv("LANE_PAUSE__3 - ASUS", "whatsapp")
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", lane._pause_wait()), daemon=True)
    t.start()
    _wait_for(lambda: (1, "whatsapp") in L.PAUSED_NOW)
    assert not gate.holds(1)                       # W136: no slot held while paused
    assert 1 in L.paused_jobs() and A._job_lane_paused(1)
    assert gate.acquire(2, lambda: False, lambda m: None)   # another job may use the lane meanwhile
    gate.release(2)
    assert t.is_alive()
    monkeypatch.setenv("LANE_PAUSE__3 - ASUS", "none")       # unpause
    t.join(2.0)
    assert out["r"] is True and not t.is_alive()
    assert lane._slot_parked                        # `_slot_resume` takes the slot back
    assert lane._slot_resume() and gate.holds(1)
    assert not L.PAUSED_NOW


def test_other_lane_paused_does_not_block(monkeypatch):
    lane, gate = _lane_with_gate(monkeypatch)
    monkeypatch.setenv("LANE_PAUSE__3 - ASUS", "maps,websites")
    assert lane._pause_wait() is True and gate.holds(1)


def test_stop_while_paused_returns_false(monkeypatch):
    lane, gate = _lane_with_gate(monkeypatch)
    monkeypatch.setenv("LANE_PAUSE", "whatsapp")
    lane.stopped = lambda: True
    assert lane._pause_wait() is False
    assert not L.PAUSED_NOW and not gate.holds(1)


class _Cloud:
    def __init__(self, cfg):
        self.cfg = cfg

    def config(self):
        return dict(self.cfg)


def test_refresh_applies_and_clears_lane_pause_quickly(monkeypatch):
    monkeypatch.setattr(A, "_CLOUD_ENV", set())
    monkeypatch.setattr(A, "_last_config_refresh", [0.0])
    monkeypatch.setattr(A, "_last_pause_refresh", [0.0])
    cloud = _Cloud({"lane_pause__3 - asus": "maps", "wa_delay__3 - asus": "1-2"})
    monkeypatch.delenv("WA_DELAY__3 - ASUS", raising=False)
    A._refresh_cloud_config(cloud, force=True)
    assert os.environ["LANE_PAUSE__3 - ASUS"] == "maps"
    # within the 5-min window: only the pause knob is re-read once LANE_PAUSE_REFRESH_SEC passed
    cloud.cfg = {"wa_delay__3 - asus": "5-12"}                # pause row deleted on the CRM
    A._last_pause_refresh[0] = time.monotonic() - A.LANE_PAUSE_REFRESH_SEC - 1
    A._refresh_cloud_config(cloud)
    assert os.environ.get("LANE_PAUSE__3 - ASUS") == ""
    assert os.environ["WA_DELAY__3 - ASUS"] == "1-2"          # other knobs keep the 5-min cadence
    monkeypatch.delenv("WA_DELAY__3 - ASUS", raising=False)
