"""W170 (2026-10-07, machine "5 - MI"): the W152 relink re-probe was gated by the W151 enrichment rule
(`browser_fallback_allowed`, no extra Chrome >= 80 % RAM) while the WhatsApp lane itself runs under the
looser W162 gate (`memory_high_for_whatsapp`, WA_HOLD_MEM_PCT = 88 %). Maps held MI at 84-87 % for hours:
every 5-min re-probe logged "memory is high", the false NEEDS RELINK flag never cleared, the lane sat
idle, the job's WA pass gave up after 15 min and the CRM told the owner to press Start session.

Now the re-probe uses the lane's own gate and is forced once after WA_REPROBE_FORCE_MIN minutes of
deferral. Fakes only: no Playwright, no Chrome, no psutil reading.
"""
from __future__ import annotations

import pytest

from webscraper import enrich as E, lanes as L
from tests.test_w144_lane_caps import _Clock, _relink_lane


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(L, "_REPROBE_DEFERRED_SINCE", None)
    monkeypatch.delenv("WA_HOLD_MEM_PCT", raising=False)
    monkeypatch.delenv("WA_REPROBE_FORCE_MIN", raising=False)
    # the old gate must no longer matter: say "no" and prove the probe still runs when the WA gate allows
    monkeypatch.setattr(E, "browser_fallback_allowed", lambda: False)
    yield


def _flagged(monkeypatch, pct: float):
    lane, store = _relink_lane()
    store.flagged_wa_accounts = lambda: ["main"]
    monkeypatch.setattr(E, "memory_pct_for_browser", lambda: pct)
    from webscraper import wa_verify as wv
    monkeypatch.setattr(wv, "login_in_progress", lambda name=None: False)
    return lane, store


def test_default_threshold_is_the_whatsapp_lane_gate():
    assert L.WA_HOLD_MEM_PCT == 88.0 and L.wa_hold_mem_pct() == 88.0
    assert L.WA_REPROBE_FORCE_MIN == 20.0 and L.wa_reprobe_force_min() == 20.0


def test_84_pct_ram_probes_under_the_88_pct_gate(monkeypatch):
    """(a) the MI shape: Maps at 84 % — the enrichment gate said no, the WhatsApp gate says go."""
    lane, store = _flagged(monkeypatch, 84.0)
    probes: list[str] = []
    monkeypatch.setattr(L, "_account_status", lambda name: probes.append(name) or "logged_in")
    assert L._reprobe_flagged(lane, store) is True
    assert probes == ["main"]
    assert any("false alarm" in n for n in lane.notes), lane.notes
    assert not any("memory is high" in n for n in lane.notes), lane.notes
    assert L._REPROBE_DEFERRED_SINCE is None


def test_90_pct_ram_skips_with_the_threshold_in_the_note(monkeypatch):
    """(b) above the lane's own gate the probe still waits, and the note says which gate."""
    lane, store = _flagged(monkeypatch, 90.0)
    monkeypatch.setattr(L, "_relink_now", _Clock(step=1.0))
    monkeypatch.setattr(L, "_account_status", lambda name: pytest.fail("must not probe"))
    assert L._reprobe_flagged(lane, store) is False
    assert any("memory is high" in n and "RAM >= 88%" in n for n in lane.notes), lane.notes
    assert L._REPROBE_DEFERRED_SINCE is not None, "the deferral clock started"


def test_90_pct_ram_but_deferred_20_min_forces_one_probe(monkeypatch):
    """(c) a flag parked behind the memory gate for >= WA_REPROBE_FORCE_MIN is probed anyway, once."""
    lane, store = _flagged(monkeypatch, 90.0)
    clock = _Clock(step=5 * 60.0)                               # each call = 5 min (the re-probe cadence)
    monkeypatch.setattr(L, "_relink_now", clock)
    probes: list[str] = []
    monkeypatch.setattr(L, "_account_status", lambda name: probes.append(name) or "logged_in")
    skipped = 0
    forced = False
    for _ in range(10):
        if L._reprobe_flagged(lane, store):
            forced = True
            break
        skipped += 1
    assert forced and probes == ["main"], (skipped, probes, lane.notes)
    assert 4 <= skipped <= 5, skipped                           # ~20 min of 5-min skips, then the force
    assert any("re-probe forced after" in n and "must not stay stuck" in n for n in lane.notes), lane.notes
    assert L._REPROBE_DEFERRED_SINCE is None, "a probe that ran resets the clock"


def test_force_zero_never_forces(monkeypatch):
    lane, store = _flagged(monkeypatch, 90.0)
    monkeypatch.setenv("WA_REPROBE_FORCE_MIN", "0")
    monkeypatch.setattr(L, "_relink_now", _Clock(step=3600.0))
    monkeypatch.setattr(L, "_account_status", lambda name: pytest.fail("must not probe"))
    for _ in range(3):
        assert L._reprobe_flagged(lane, store) is False
