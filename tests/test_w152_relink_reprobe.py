"""W152 (CRM T1037, 2026-10-07): three fixes from the morning's `status lf`.

  * ASUS / MI / DELL carried a W143 `needs_relink` flag set during the T1045 RAM thrash (the sync never
    finished because the MACHINE was frozen). After the restart the sessions were fine — the owner's
    "Start session" showed the chat list, no QR — yet three WhatsApp lanes had waited 15 min each and
    ended `wa_no_session`. Now: any `logged_in` sighting clears the flag (Store.set_wa_status), and the
    relink wait re-probes flagged accounts on entry + every WA_RELINK_REPROBE_SEC and resumes itself.
  * The enrichment lane ended `error:AI research: nvidia: could not reach the API` after 316/316 sites
    because ONE transient miss in a 1-4 lead batch tripped `>= len // 2`. The verdict is cumulative now
    (>= RESEARCH_ERROR_MIN_FAILED all-provider failures AND >= 50 % of researched leads).
  * `httpx.InvalidURL` (a redirect to "http://host.compath") is not an HTTPError and killed MI's #22525
    enrichment lane twice. `_fetch_ex` now reports it as `bad_url`.

Fakes only: no Playwright, no Chrome, no network.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from webscraper import enrich as E, lanes as L
from webscraper.store import Store
from tests.test_w144_lane_caps import _Clock, _relink_lane


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "test.db"
    return lambda: Store(path)


# ── (a) store: a logged-in sighting clears the flag ──────────────────────────────────
def test_logged_in_sighting_clears_needs_relink(db):
    s = db()
    s.add_wa_account("main")
    s.set_wa_needs_relink("main", True)
    assert s.flagged_wa_accounts() == ["main"]
    assert s.enabled_wa_accounts() == []
    s.set_wa_status("main", "logged_out")                 # a QR screen keeps the flag
    assert s.flagged_wa_accounts() == ["main"]
    s.set_wa_status("main", "logged_in")                  # the chat list ends it
    assert s.flagged_wa_accounts() == []
    assert s.enabled_wa_accounts() == ["main"]
    row = next(r for r in s.list_wa_accounts() if r["name"] == "main")
    assert not row["needs_relink"] and row["needs_relink_at"] is None
    assert s.wa_relinked_since("2000-01-01T00:00:00+00:00")
    s.close()


# ── (b) the relink wait re-probes and resumes by itself ──────────────────────────────
def _flagged(lane_store, names, probe):
    lane, store = lane_store
    store.flagged_wa_accounts = lambda: list(names)
    store.wa_relinked_since = lambda since: False
    return lane, store


def test_relink_wait_resumes_when_the_probe_sees_the_chat_list(monkeypatch):
    L.reset_stage_gates()
    monkeypatch.setattr(L, "WA_RELINK_POLL_SEC", 0.001)
    monkeypatch.setenv("WA_NO_SESSION_GIVE_UP_SEC", "900")
    monkeypatch.setattr(L, "_relink_now", _Clock(step=1.0))
    probes: list[str] = []
    monkeypatch.setattr(L, "_account_status", lambda name: probes.append(name) or "logged_in")
    monkeypatch.setattr(L, "memory_high_for_whatsapp", lambda: False)   # W170 gate
    lane, store = _flagged(_relink_lane(), ["main"], None)
    gate = L.STAGE_GATES["whatsapp"]
    assert gate.acquire(1, lambda: False, lambda m: None)
    out = L._wait_for_relink(lane, store, RuntimeError("WhatsApp account(s) need a fresh QR relink: main"))
    assert out is True, out
    assert probes == ["main"], "one probe on entry was enough"
    assert gate.holds(1), "the lane took its slot back on resume"
    assert any("false alarm" in n and "resuming" in n for n in lane.notes), lane.notes


def test_relink_wait_reprobes_on_the_interval_then_gives_up(monkeypatch):
    """A probe that still sees the QR does not resume; probes repeat every WA_RELINK_REPROBE_SEC and the
    W144 give-up still ends the wait `wa_no_session`."""
    L.reset_stage_gates()
    monkeypatch.setattr(L, "WA_RELINK_POLL_SEC", 0.001)
    monkeypatch.setenv("WA_NO_SESSION_GIVE_UP_SEC", "900")
    monkeypatch.setenv("WA_RELINK_REPROBE_SEC", "300")
    monkeypatch.setattr(L, "_relink_now", _Clock(step=50.0))   # each poll = 50 s
    probes: list[str] = []
    monkeypatch.setattr(L, "_account_status", lambda name: probes.append(name) or "logged_out")
    monkeypatch.setattr(L, "memory_high_for_whatsapp", lambda: False)   # W170 gate
    lane, store = _flagged(_relink_lane(), ["main"], None)
    out = L._wait_for_relink(lane, store, RuntimeError("WhatsApp account(s) need a fresh QR relink: main"))
    assert out == L.R_WA_NO_SESSION
    assert 3 <= len(probes) <= 5, probes                     # entry + ~every 300 s of a 900 s window
    assert any("still needs a QR relink" in n for n in lane.notes), lane.notes


def test_reprobe_skips_high_memory_and_open_logins(monkeypatch):
    lane, store = _flagged(_relink_lane(), ["main"], None)
    monkeypatch.setattr(L, "_account_status", lambda name: pytest.fail("must not probe"))
    monkeypatch.setattr(L, "_REPROBE_DEFERRED_SINCE", None)
    monkeypatch.setattr(L, "memory_high_for_whatsapp", lambda: True)   # W170: >= WA_HOLD_MEM_PCT (88 %)
    assert L._reprobe_flagged(lane, store) is False
    assert any("memory is high" in n for n in lane.notes), lane.notes
    monkeypatch.setattr(L, "memory_high_for_whatsapp", lambda: False)
    from webscraper import wa_verify as wv
    monkeypatch.setattr(wv, "login_in_progress", lambda name=None: True)  # the owner is scanning
    assert L._reprobe_flagged(lane, store) is False


def test_reprobe_errors_never_end_the_wait(monkeypatch):
    lane, store = _flagged(_relink_lane(), ["main"], None)
    monkeypatch.setattr(L, "memory_high_for_whatsapp", lambda: False)   # W170 gate

    def boom(name):
        raise RuntimeError("chrome exploded")
    monkeypatch.setattr(L, "_account_status", boom)
    assert L._reprobe_flagged(lane, store) is False


def test_reprobe_is_a_no_op_without_flagged_accounts(monkeypatch):
    lane, store = _relink_lane()                              # the W144 fake has no flagged_wa_accounts
    monkeypatch.setattr(L, "_account_status", lambda name: pytest.fail("nothing to probe"))
    assert L._reprobe_flagged(lane, store) is False


# ── (c) cumulative AI-research verdict ───────────────────────────────────────────────
class _RStore:
    def __init__(self, keys):
        self._keys = keys
        self.job = {"research_done": 0}

    def places(self, job_id):
        return [{"place_key": k, "website": f"http://{k}.example"} for k in self._keys]

    def get_job(self, job_id):
        return self.job

    def update_job(self, job_id, **kw):
        self.job.update(kw)

    def record_phase_rate(self, *a, **k):
        pass

    def log(self, *a, **k):
        pass


def _enrich_lane(store):
    lane = L.EnrichmentLane.__new__(L.EnrichmentLane)
    L.Lane.__init__(lane, 1, {"do_research": 1}, ctl=None)
    lane.store = store
    lane.stopped = lambda: False
    return lane


def _run_batch(monkeypatch, lane, store, n, failed, ai_failed, error="nvidia: could not reach the API"):
    from webscraper import research as R

    async def fake(store_, targets, conc, on_progress, should_stop):
        out = {"done": len(targets) - failed, "skipped": 0, "failed": failed}
        if ai_failed:
            out["gemini_failed"] = ai_failed
            out["error"] = error
        return out
    monkeypatch.setattr(R, "research_places", fake)
    store._keys = [f"k{i}" for i in range(n)]
    lane._research([{"place_key": k} for k in store._keys])


def test_one_transient_miss_in_a_small_batch_does_not_fail_the_lane(monkeypatch):
    store = _RStore([])
    lane = _enrich_lane(store)
    _run_batch(monkeypatch, lane, store, n=2, failed=1, ai_failed=1)       # the #22556 shape
    assert lane.research_error is None
    for _ in range(10):                                                     # 10 clean batches of 4
        _run_batch(monkeypatch, lane, store, n=4, failed=0, ai_failed=0)
    _run_batch(monkeypatch, lane, store, n=1, failed=1, ai_failed=1)       # another lone miss
    assert lane.research_error is None
    assert lane._research_targets == 43 and lane._research_ai_failed == 2


def test_a_real_outage_still_ends_the_lane_with_the_reason(monkeypatch):
    store = _RStore([])
    lane = _enrich_lane(store)
    for _ in range(3):                                                      # job #17: every call 429s
        _run_batch(monkeypatch, lane, store, n=2, failed=2, ai_failed=2,
                   error="Gemini quota exhausted (HTTP 429)")
    assert lane.research_error == "Gemini quota exhausted (HTTP 429)"
    assert L.RESEARCH_ERROR_MIN_FAILED == 5
    # recovery later in the lane lifts the verdict again
    for _ in range(4):
        _run_batch(monkeypatch, lane, store, n=4, failed=0, ai_failed=0)
    assert lane.research_error is None


# ── (d) a malformed redirect is one bad site, not a dead lane ────────────────────────
def test_invalid_url_is_reported_as_bad_url_not_raised():
    class _T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.InvalidURL("For absolute URLs, path must be empty or begin with '/'")

    async def go():
        async with httpx.AsyncClient(transport=_T()) as client:
            return await E._fetch_ex(client, "http://www.godrejhospital.com/", retries=0)
    got = asyncio.run(go())
    assert got.html is None and got.error == "bad_url"
    assert "bad_url" in L._ENRICH_ERROR_EXPLAIN
