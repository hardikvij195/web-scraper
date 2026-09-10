"""W108 (CRM T566): two faults seen on the Mac, 2026-09-10 17:43–17:49.

A. A WhatsApp browser died mid-number, `_check` relaunched it, and the fresh profile sat on
   WhatsApp's "messages are downloading" splash for the whole sync wait — `WaUnavailable`
   escaped the run and failed an entire re-verify (129 numbers done, three healthy accounts).
   Now it skips that account for the run, exactly like the same condition at session start.
B. The re-verify ran inside `_tick` on the main loop, which is what heartbeats and polls
   commands — the machine looked offline and Restart went unanswered for 6 minutes. Now it
   runs on its own thread, and update/restart wait for it like they wait for a normal job.

Fakes only: no Playwright, no Chrome, no `data/leads.db`.
"""
from __future__ import annotations

import threading

import pytest

from webscraper import agent
from webscraper import wa_verify as wv


# ── A: WaUnavailable out of a check ──────────────────────────────────────────────────
class _Ctx:
    def close(self) -> None:
        pass


class _Page:
    def __init__(self, acct: str) -> None:
        self.acct = acct

    def goto(self, url, **kw):
        pass


class _PW:
    def __init__(self) -> None:
        self.started = self.stopped = 0

    def start(self):
        self.started += 1
        return self

    def stop(self):
        self.stopped += 1


class _Conn:
    def execute(self, *a): return self
    def commit(self): pass


class _Store:
    """Two linked accounts; rotation skips the ones the run marked unavailable."""
    conn = _Conn()

    def list_wa_accounts(self):
        return [{"name": "acc1", "disabled": 0}, {"name": "acc2", "disabled": 0}]

    def pick_wa_account(self, cap, today, exclude=()):
        return next((n for n in ("acc1", "acc2") if n not in (exclude or ())), None)

    def bump_wa_account(self, name, today): pass


def _rows(n: int) -> list[dict]:
    return [{"place_key": f"p{i}", "number": f"+91987654{i:04d}", "source": "maps"} for i in range(n)]


@pytest.fixture()
def wa(monkeypatch):
    pw = _PW()
    monkeypatch.setattr(wv, "sync_playwright", lambda: pw)
    monkeypatch.setattr(wv, "_dismiss_popup", lambda page: None)
    monkeypatch.setattr(wv.settings, "wa_delay_min", 0.0)
    monkeypatch.setattr(wv.settings, "wa_delay_max", 0.0)
    monkeypatch.setattr(wv, "_ensure_session",
                        lambda pw, open_ctx, rl, name, headless=None: open_ctx.setdefault(name, (_Ctx(), _Page(name)))[1])
    return pw


def _still_syncing_on(acct: str):
    def decide(page):
        if page.acct == acct:
            raise wv.WaUnavailable(f"[{acct}] WhatsApp Web is still downloading messages after 360s — skipped this run (still linked)")
        return "yes"
    return decide


def test_a_still_syncing_account_is_skipped_and_the_run_continues(wa, monkeypatch):
    monkeypatch.setattr(wv, "_decide", _still_syncing_on("acc1"))
    res = wv.verify_places(_Store(), _rows(5))          # must not raise
    # the number in flight on acc1 stays unchecked; acc2 decides the other four
    assert res["checked"] == 4 and res["yes"] == 4
    assert wa.started == 1 and wa.stopped == 1


def test_a_pinned_slice_ends_cleanly_when_its_account_is_still_syncing(wa, monkeypatch):
    monkeypatch.setattr(wv, "_decide", _still_syncing_on("acc1"))
    res = wv.verify_places(_Store(), _rows(5), account="acc1")
    assert res["checked"] == 0
    assert wa.stopped == 1


# ── B: re-verify off the main loop ───────────────────────────────────────────────────
@pytest.fixture()
def clean_reverify(monkeypatch):
    monkeypatch.setitem(agent._REVERIFY, "thread", None)
    monkeypatch.setitem(agent._REVERIFY, "job", None)
    monkeypatch.setattr(agent, "_DEFERRED_CMD", [None])
    monkeypatch.setattr(agent, "_cmd_thread", None)
    monkeypatch.setattr(agent.srv.worker, "current_job", None)

    class _FakeStore:
        def close(self): pass
    monkeypatch.setattr(agent, "Store", _FakeStore)


def _blocking_reverify(monkeypatch):
    gate = threading.Event()
    started = threading.Event()

    def fake(cloud, store, jid, leads_verify=False):
        started.set()
        gate.wait(5)
    monkeypatch.setattr(agent, "_reverify_wa", fake)
    return gate, started


def test_reverify_busy_follows_the_thread(clean_reverify, monkeypatch):
    gate, started = _blocking_reverify(monkeypatch)
    assert agent._reverify_busy() is None
    agent._start_reverify(object(), 6621, False)
    assert started.wait(2)
    assert agent._reverify_busy() == 6621
    gate.set()
    agent._REVERIFY["thread"].join(2)
    assert agent._reverify_busy() is None


def test_run_deferred_waits_for_a_running_reverify(clean_reverify, monkeypatch):
    calls = []
    monkeypatch.setattr(agent, "_do_update", lambda cloud, cmd_id: calls.append("update") or (True, "x"))
    gate, started = _blocking_reverify(monkeypatch)
    agent._DEFERRED_CMD[0] = "update"
    agent._start_reverify(object(), 6621, False)
    assert started.wait(2)
    assert agent._run_deferred(object()) is False and calls == []
    gate.set()
    agent._REVERIFY["thread"].join(2)
    assert agent._run_deferred(object()) is True and calls == ["update"]


def test_defer_command_uses_the_cloud_id_of_a_reverify(clean_reverify, monkeypatch):
    monkeypatch.setattr(agent, "_cloud_job_id", lambda local_id: pytest.fail("a re-verify has no local jobs row"))
    ok, result = agent._defer_command({"command": "restart"}, 6621, cloud_job=6621)
    assert ok and result == "deferred — will restart after job #6621 finishes"
    assert agent._DEFERRED_CMD[0] == "restart"


class _TickCloud:
    def __init__(self, jobs):
        self._jobs = jobs
        self.claims: list[int] = []

    def jobs(self):
        return self._jobs

    def claim(self, jid):
        self.claims.append(jid)
        return {"id": jid}


class _TickStore:
    class _C:
        def execute(self, *a):
            return self

        def fetchall(self):
            return []
    conn = _C()


def test_tick_runs_one_reverify_and_claims_nothing_beside_it(clean_reverify, monkeypatch):
    gate, started = _blocking_reverify(monkeypatch)
    cloud = _TickCloud([{"id": 6621, "wa_verify_only": True, "status": "queued"}])
    agent._tick(cloud, _TickStore(), "crm", {})
    assert started.wait(2) and cloud.claims == [6621]
    # the main loop comes round again while the re-check runs: the same (possibly stale)
    # re-verify and a fresh scrape job are both on offer — neither is claimed
    cloud._jobs = [{"id": 6621, "wa_verify_only": True, "status": "running"},
                   {"id": 7001, "status": "queued", "query": "q"}]
    agent._tick(cloud, _TickStore(), "crm", {})
    assert cloud.claims == [6621]
    gate.set()
    agent._REVERIFY["thread"].join(2)
