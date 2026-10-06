"""W144 (CRM T1022 / T1024): no job cap — per-lane caps schedule jobs, WhatsApp gives up without a session.

  * `job_next_lane`: the lane a queued job occupies first (discovery > enrichment > whatsapp > None).
  * `may_start_job`: a job starts only when THAT lane has a free slot now; crash guard, memory block,
    WhatsApp without a usable account.
  * `capacity()`: `maps_free` / `enrich_free` / `wa_free` / `lane_slots` / `hard_max`.
  * the W110 relink wait ends `wa_no_session` after `WA_NO_SESSION_GIVE_UP_SEC` (fake clock), and the
    WhatsApp lane ends with that reason carrying its leftover counters.
"""
from __future__ import annotations

import threading

import pytest

from webscraper import lanes as L
from webscraper import server as server_mod
from webscraper.server import (LANE_DISCOVERY, LANE_ENRICHMENT, LANE_WHATSAPP, job_next_lane,
                               may_start_job)

# the W110 harness (one pending number, verify refuses until linked) + the `db` fixture it uses
from test_lanes import _relink_setup, db  # noqa: F401,E402


# ── (a) next lane ────────────────────────────────────────────────────────────────────
def test_job_next_lane_order():
    fresh = {"reenrich_only": 0, "do_enrich": 1, "do_wa_verify": 1}
    assert job_next_lane(fresh) == LANE_DISCOVERY                       # needs Maps first
    assert job_next_lane({"reenrich_only": 1, "discovery_pending": 1}) == LANE_DISCOVERY   # W62 resume
    re = {"reenrich_only": 1, "do_enrich": 1, "do_wa_verify": 1}
    assert job_next_lane(re) == LANE_ENRICHMENT                         # unknown counts = assume work
    assert job_next_lane(re, enrich_pending=5, wa_pending=0) == LANE_ENRICHMENT
    assert job_next_lane(re, enrich_pending=0, wa_pending=3) == LANE_WHATSAPP   # websites done, numbers left
    assert job_next_lane(re, enrich_pending=0, wa_pending=0) is None
    wa_only = {"reenrich_only": 1, "wa_verify_only": 1, "do_enrich": 1}
    assert job_next_lane(wa_only, enrich_pending=9, wa_pending=2) == LANE_WHATSAPP
    assert job_next_lane(wa_only, enrich_pending=9, wa_pending=0) is None
    no_wa = {"reenrich_only": 1, "do_enrich": 1, "do_wa_verify": 0}
    assert job_next_lane(no_wa, enrich_pending=0, wa_pending=7) is None


# ── (b) start rule ───────────────────────────────────────────────────────────────────
def _ok(**kw):
    base = dict(disc_free=True, enrich_free=1, wa_free=1, wa_account=True, n_inflight=0, max_inflight=8)
    base.update(kw)
    return base


def test_start_rule_per_lane():
    assert may_start_job(LANE_DISCOVERY, **_ok()) is True
    assert may_start_job(LANE_DISCOVERY, **_ok(disc_free=False)) is False
    # a full Maps tab does not stop an enrichment-next job, and vice versa
    assert may_start_job(LANE_ENRICHMENT, **_ok(disc_free=False)) is True
    assert may_start_job(LANE_ENRICHMENT, **_ok(enrich_free=0, wa_free=3)) is False   # other lanes idle: still no
    assert may_start_job(LANE_WHATSAPP, **_ok(enrich_free=0)) is True
    assert may_start_job(LANE_WHATSAPP, **_ok(wa_free=0)) is False
    assert may_start_job(None, **_ok(disc_free=False, enrich_free=0, wa_free=0)) is True   # nothing to do: closes


def test_start_rule_crash_guard_and_memory_and_account():
    assert may_start_job(LANE_DISCOVERY, **_ok(n_inflight=8, max_inflight=8)) is True     # W149: one overflow slot for Maps
    assert may_start_job(LANE_DISCOVERY, **_ok(n_inflight=9, max_inflight=8)) is False
    assert may_start_job(LANE_WHATSAPP, **_ok(n_inflight=8, max_inflight=8)) is False     # lane-only jobs: no overflow
    assert may_start_job(LANE_ENRICHMENT, **_ok(n_inflight=7, max_inflight=8)) is True
    assert may_start_job(LANE_ENRICHMENT, **_ok(mem_blocked=True)) is False
    assert may_start_job(LANE_WHATSAPP, **_ok(wa_account=False)) is False       # no linked/enabled/unpaused account
    assert may_start_job(LANE_DISCOVERY, **_ok(wa_account=False)) is True       # only WhatsApp needs one


def test_w150_unstartable_whatsapp_job_is_released_after_the_grace_period():
    from webscraper.server import LANE_WHATSAPP, unstartable_release_due
    assert unstartable_release_due(LANE_WHATSAPP, False, True, 119) is False
    assert unstartable_release_due(LANE_WHATSAPP, False, True, 120) is True
    assert unstartable_release_due(LANE_WHATSAPP, True, True, 999) is False      # an account is usable: just wait for the slot
    assert unstartable_release_due(LANE_WHATSAPP, False, False, 999) is False    # local CLI job: nothing to hand back
    assert unstartable_release_due(LANE_ENRICHMENT, False, True, 999) is False   # other lanes are not account-bound


def test_max_inflight_is_a_crash_guard(monkeypatch):
    monkeypatch.delenv("MAX_INFLIGHT_JOBS", raising=False)
    monkeypatch.setattr(server_mod, "DEVICE_NAME", "", raising=False)
    import webscraper.agent as agent_mod
    monkeypatch.setattr(agent_mod, "DEVICE_NAME", "")
    assert server_mod.max_inflight_jobs() == 8
    monkeypatch.setenv("MAX_INFLIGHT_JOBS", "50")
    assert server_mod.max_inflight_jobs() == 12


# ── (c) capacity() ───────────────────────────────────────────────────────────────────
class _Row(dict):
    """sqlite3.Row-ish: `r["k"]` and `.get` both work."""


class _FakeStore:
    queued: list = []
    accounts: list = ["main"]
    enr_pending: int = 5
    wa_pending: int = 5

    def queued_jobs(self):
        return list(self.queued)

    def enabled_wa_accounts(self):
        return list(self.accounts)

    def count_pending_enrichment(self, jid):
        return self.enr_pending

    def count_wa_pending(self, jid):
        return self.wa_pending

    def close(self):
        pass


@pytest.fixture()
def cap_env(monkeypatch):
    monkeypatch.setattr(server_mod, "Store", lambda *a, **k: _FakeStore())
    monkeypatch.setattr(server_mod, "memory_blocked", lambda: False)
    monkeypatch.setattr(server_mod, "_cached_memory", lambda: {"used_pct": 40})
    monkeypatch.setattr(server_mod, "max_inflight_jobs", lambda: 8)
    monkeypatch.setenv("LANE_SLOTS_ENRICHMENT", "2")
    monkeypatch.setenv("LANE_SLOTS_WHATSAPP", "1")
    from webscraper import wa_verify
    monkeypatch.setattr(wa_verify, "_PAUSED", set())
    L.reset_stage_gates()
    _FakeStore.queued = []
    _FakeStore.accounts = ["main"]
    yield
    L.reset_stage_gates()


def test_capacity_all_idle(cap_env):
    w = server_mod.Worker()
    c = w.capacity()
    assert c["maps_free"] and c["enrich_free"] and c["wa_free"] and c["lanes_free"]
    assert c["discovery_free"] is True
    assert c["max_inflight"] == 8 and c["hard_max"] == 8
    assert c["lane_slots"] == {"discovery": {"busy": 0, "cap": 1},
                               "enrichment": {"busy": 0, "cap": 2},
                               "whatsapp": {"busy": 0, "cap": 1, "account": True}}


def test_capacity_per_lane_busy(cap_env):
    w = server_mod.Worker()
    # Maps tab taken -> maps_free off, the other two lanes still offered.
    w._inflight[1] = None
    w._disc_job = 1
    w._start_lane[1] = LANE_DISCOVERY
    c = w.capacity()
    assert c["maps_free"] is False and c["enrich_free"] is True and c["wa_free"] is True
    assert c["lane_slots"]["discovery"]["busy"] == 1 and c["inflight"] == 1

    # WhatsApp gate (1 slot) held by a running lane -> wa_free off; enrichment: one holder + one
    # queued of 2 slots -> full.
    L.STAGE_GATES["whatsapp"].acquire(1, lambda: False, lambda m: None)
    L.STAGE_GATES["enrichment"].acquire(1, lambda: False, lambda m: None)
    L.STAGE_GATES["enrichment"]._queue.append(2)              # a second job waiting on the gate
    c = w.capacity()
    assert c["wa_free"] is False and c["enrich_free"] is False and c["maps_free"] is False
    assert c["lanes_free"] is False
    assert c["lane_slots"]["whatsapp"] == {"busy": 1, "cap": 1, "account": True}
    assert c["lane_slots"]["enrichment"] == {"busy": 2, "cap": 2}
    L.STAGE_GATES["enrichment"]._queue.clear()
    L.STAGE_GATES["enrichment"].release(1)
    L.STAGE_GATES["whatsapp"].release(1)

    # A promised start (job started for its enrichment lane, Pipeline not built yet) counts.
    w._disc_job = None
    w._start_lane[1] = LANE_ENRICHMENT
    c = w.capacity()
    assert c["maps_free"] is True and c["lane_slots"]["enrichment"]["busy"] == 1 and c["enrich_free"] is True
    w._start_lane[2] = LANE_ENRICHMENT
    w._inflight[2] = None
    assert w.capacity()["enrich_free"] is False


def test_capacity_counts_mirrored_unstarted_jobs_against_their_lane(cap_env):
    w = server_mod.Worker()
    _FakeStore.queued = [_Row(id=7, cloud_id="c7", reenrich_only=0, do_enrich=1, do_wa_verify=1)]
    c = w.capacity()
    assert c["maps_free"] is False and c["enrich_free"] is True and c["wa_free"] is True
    assert c["inflight"] == 1 and c["started"] == 0
    _FakeStore.queued = [_Row(id=8, cloud_id="c8", reenrich_only=1, wa_verify_only=1, do_wa_verify=1)]
    c = w.capacity()
    assert c["maps_free"] is True and c["wa_free"] is False and c["enrich_free"] is True


def test_capacity_whatsapp_needs_a_usable_account(cap_env, monkeypatch):
    w = server_mod.Worker()
    _FakeStore.accounts = []
    c = w.capacity()
    assert c["wa_free"] is False and c["lane_slots"]["whatsapp"]["account"] is False
    assert c["maps_free"] is True and c["enrich_free"] is True and c["lanes_free"] is True
    # W142: an account paused by a CRM command is not usable either
    from webscraper import wa_verify
    _FakeStore.accounts = ["main"]
    monkeypatch.setattr(wa_verify, "_PAUSED", {"main"})
    assert w.capacity()["wa_free"] is False
    monkeypatch.setattr(wa_verify, "_PAUSED", set())
    assert w.capacity()["wa_free"] is True


def test_capacity_crash_guard_and_memory_block_everything(cap_env, monkeypatch):
    w = server_mod.Worker()
    monkeypatch.setattr(server_mod, "max_inflight_jobs", lambda: 1)
    w._inflight[1] = None
    c = w.capacity()
    # W149: at the guard the Maps tab may still take ONE discovery job (overflow slot); the other lanes may not
    assert c["maps_free"] and not c["enrich_free"] and not c["wa_free"] and c["lanes_free"]
    assert c["hard_max"] == 1 and c["discovery_free"] is True       # the Maps tab itself is idle
    w._inflight[2] = None                                           # one past the guard: nothing more
    c = w.capacity()
    assert not c["maps_free"] and not c["lanes_free"]
    del w._inflight[2]
    monkeypatch.setattr(server_mod, "max_inflight_jobs", lambda: 8)
    monkeypatch.setattr(server_mod, "memory_blocked", lambda: True)
    c = w.capacity()
    assert not c["maps_free"] and not c["enrich_free"] and not c["wa_free"] and c["discovery_free"] is False


# ── (d) wa_no_session give-up ────────────────────────────────────────────────────────
class _Clock:
    def __init__(self, step: float) -> None:
        self.t = 1000.0
        self.step = step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


def _relink_lane():
    class _Store:
        def wa_relinked_since(self, since):
            return False

        def enabled_wa_accounts(self):
            return ["acct"]           # an account exists, its session just dropped (W110, not T928)

    class _Ctl:
        def enrichment_finished(self):
            return False

    class _Lane:
        key = "whatsapp"
        job_id = 1
        ctl = _Ctl()
        notes: list = []

        def note(self, m, *a, **k):
            self.notes.append(m)

        def stopped(self):
            return False

    return _Lane(), _Store()


def test_relink_wait_gives_up_after_the_window(monkeypatch):
    L.reset_stage_gates()
    monkeypatch.setattr(L, "WA_RELINK_POLL_SEC", 0.001)
    monkeypatch.setenv("WA_NO_SESSION_GIVE_UP_SEC", "900")
    monkeypatch.setattr(L, "_relink_now", _Clock(step=100.0))     # each poll = 100 s of wall clock
    lane, store = _relink_lane()
    gate = L.STAGE_GATES["whatsapp"]
    assert gate.acquire(1, lambda: False, lambda m: None)
    out = L._wait_for_relink(lane, store, RuntimeError("no WhatsApp account is logged in"))
    assert out == L.R_WA_NO_SESSION
    assert not gate.holds(1), "the parked lane handed its slot back (W123) and did not retake it"
    assert L.R_WA_NO_SESSION == "wa_no_session"
    assert "wa_no_session" not in L.Store.OK_REASONS if hasattr(L, "Store") else True


def test_relink_wait_zero_means_wait_forever(monkeypatch):
    monkeypatch.setattr(L, "WA_RELINK_POLL_SEC", 0.001)
    monkeypatch.setenv("WA_NO_SESSION_GIVE_UP_SEC", "0")
    monkeypatch.setattr(L, "_relink_now", _Clock(step=10_000.0))
    lane, store = _relink_lane()
    linked = {"v": False}
    store.wa_relinked_since = lambda since: linked["v"]
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", L._wait_for_relink(lane, store, RuntimeError("x"))))
    t.start()
    t.join(timeout=0.3)
    assert t.is_alive(), "with 0 the lane keeps waiting (pre-W144 behaviour)"
    linked["v"] = True
    t.join(timeout=5)
    assert out["r"] is True


def test_whatsapp_lane_ends_wa_no_session_with_its_leftover(db, monkeypatch):
    """T1024 end-to-end on the lane: no relink within the window while discovery still runs ->
    the lane ends `wa_no_session`, logs the hand-off line and leaves wa_verify_total = done +
    pending so the job's end carries the WhatsApp leftover (same shape as `wa_daily_cap`)."""
    from webscraper.store import Store
    state = {"calls": 0, "pending": True, "linked": False}
    job_id, job = _relink_setup(db, monkeypatch, state)
    monkeypatch.setenv("WA_NO_SESSION_GIVE_UP_SEC", "900")
    monkeypatch.setattr(L, "_relink_now", _Clock(step=300.0))
    pipe = L.Pipeline(job_id, job, lambda lane: L.R_COMPLETED, store_factory=db)   # discovery NOT done
    lane = pipe.whatsapp
    lane.store = db()
    try:
        reason = lane._work()
    finally:
        lane.store.close()
    assert reason == L.R_WA_NO_SESSION, reason
    assert state["calls"] == 1
    s = db()
    msgs = [r["message"] for r in s.logs(job_id)]
    row = s.get_job(job_id)
    s.close()
    assert any("gave up after 15 min without a linked account" in m and "machine with a session" in m
               for m in msgs), msgs
    assert int(row["wa_verify_total"] or 0) == 1 and int(row["wa_verify_done"] or 0) == 0
    assert "wa_no_session" not in Store.OK_REASONS

    from webscraper import eta
    assert eta.REASON_TEXT["wa_no_session"] == "no WhatsApp session on this machine"
