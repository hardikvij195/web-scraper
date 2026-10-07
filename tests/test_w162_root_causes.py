"""W162 / W163 / W164 (CRM T1047, root causes of the 2026-10-07 incidents).

W162 the WhatsApp lane holds (no Chrome, slot released) while Maps still runs on a machine at/above
     WA_HOLD_MEM_PCT — three Chromes on 8 GB are what made WhatsApp Web re-sync on every load.
W163 `login()` never wipes a profile because WhatsApp Web "did not render" (that is the machine, not
     the link) and records what it saw for the CRM command result.
W164 `capacity().wa_free` is false while an in-flight job's WhatsApp lane still has numbers, even when
     that lane holds no slot right now.
"""
from __future__ import annotations

from pathlib import Path

from webscraper import enrich as E, lanes as L, server as S, wa_verify as wv
from webscraper.store import Store


# ── W162 ────────────────────────────────────────────────────────────────────────────
def test_memory_high_for_whatsapp(monkeypatch):
    monkeypatch.delenv("WA_HOLD_MEM_PCT", raising=False)
    assert L.wa_hold_mem_pct() == 88.0
    monkeypatch.setattr(E, "memory_pct_for_browser", lambda: 91.0)
    assert L.memory_high_for_whatsapp()
    monkeypatch.setattr(E, "memory_pct_for_browser", lambda: 70.0)
    assert not L.memory_high_for_whatsapp()
    monkeypatch.setattr(E, "memory_pct_for_browser", lambda: 0.0)   # unknown reading: never "high"
    assert not L.memory_high_for_whatsapp()
    monkeypatch.setenv("WA_HOLD_MEM_PCT", "60")
    monkeypatch.setattr(E, "memory_pct_for_browser", lambda: 65.0)
    assert L.memory_high_for_whatsapp()


# ── W163 ────────────────────────────────────────────────────────────────────────────
def test_login_keeps_the_profile_when_whatsapp_web_does_not_render(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(wv.settings, "wa_profiles_dir", tmp_path / "profiles")
    prof = wv.profile_dir("main"); prof.mkdir(parents=True, exist_ok=True); (prof / "Default").mkdir(exist_ok=True)
    (prof / "Default" / "keep.txt").write_text("linked session data")
    monkeypatch.setattr(wv, "Store", lambda *a, **k: _NoStore())
    monkeypatch.setattr(wv, "sync_playwright", lambda: _PW())
    monkeypatch.setattr(wv, "_login_attempt", lambda pw, name, browser=None: None)   # "blank" twice
    monkeypatch.setattr(wv, "browser_for", lambda name: "chrome")
    monkeypatch.setattr(wv, "other_browser", lambda cur: "chromium")
    wv.LAST_LOGIN_RESULT.clear()
    assert wv.login("main") is False
    assert (prof / "Default" / "keep.txt").exists(), "W163: the profile must never be wiped for 'did not render'"
    assert "did not render" in wv.LAST_LOGIN_RESULT["main"] and "kept" in wv.LAST_LOGIN_RESULT["main"]


class _NoStore:
    def add_wa_account(self, name): pass
    def set_wa_needs_relink(self, name, flag): pass
    def set_wa_status(self, name, status): pass


class _PW:
    def __enter__(self): return self
    def __exit__(self, *a): return False


# ── W164 ────────────────────────────────────────────────────────────────────────────
def test_wa_free_false_while_an_inflight_job_still_has_numbers(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("LANE_ONE_JOB_PER_LANE", raising=False)

    class _Lane:
        def __init__(self, enabled=True, done=False):
            self._e = enabled
            import threading
            self.done = threading.Event()
            if done: self.done.set()
        def enabled(self): return self._e

    class _Pipe:
        def __init__(self, job_id, wa_enabled=True, wa_done=False):
            self.job_id = job_id
            self.whatsapp = _Lane(wa_enabled, wa_done)

    class _St:
        def __init__(self, pending): self.pending = pending
        def count_wa_pending(self, jid): return self.pending.get(jid, 0)

    w = S.Worker.__new__(S.Worker)
    import threading
    w._lock = threading.Lock()
    w._inflight = {1: _Pipe(1), 2: None}
    assert w._wa_lane_claimed(_St({1: 40})) is True            # idle / parked lane, numbers left
    assert w._wa_lane_claimed(_St({1: 0})) is False            # nothing left: free for a WA-only job
    w._inflight = {1: _Pipe(1, wa_done=True)}
    assert w._wa_lane_claimed(_St({1: 40})) is False           # lane ended: free
    w._inflight = {1: _Pipe(1, wa_enabled=False)}
    assert w._wa_lane_claimed(_St({1: 40})) is False
    monkeypatch.setenv("LANE_ONE_JOB_PER_LANE", "0")
    w._inflight = {1: _Pipe(1)}
    assert w._wa_lane_claimed(_St({1: 40})) is False           # old W144 semantics untouched


# ── W167 ────────────────────────────────────────────────────────────────────────────
def test_lane_claimed_covers_enrichment_too(monkeypatch):
    monkeypatch.delenv("LANE_ONE_JOB_PER_LANE", raising=False)
    import threading

    class _Lane:
        def __init__(self, done=False):
            self.done = threading.Event()
            if done: self.done.set()
        def enabled(self): return True

    class _Pipe:
        def __init__(self, job_id):
            self.job_id = job_id
            self.whatsapp = _Lane(); self.enrichment = _Lane()

    class _St:
        def __init__(self, enr, wa): self.enr, self.wa = enr, wa
        def count_pending_enrichment(self, jid): return self.enr
        def count_wa_pending(self, jid): return self.wa

    w = S.Worker.__new__(S.Worker)
    w._lock = threading.Lock()
    w._inflight = {1: _Pipe(1)}
    assert w._lane_claimed(_St(12, 0), S.LANE_ENRICHMENT) is True
    assert w._lane_claimed(_St(0, 0), S.LANE_ENRICHMENT) is False
    assert w._lane_claimed(_St(0, 7), S.LANE_WHATSAPP) is True
    assert w._lane_claimed(_St(5, 5), S.LANE_DISCOVERY) is False
