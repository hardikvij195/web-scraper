"""T596: a WhatsApp re-verify can be stopped, and a Mac is never released.

2026-09-12: "stop all jobs" parked the Mac, but its leads-verify #6984 kept checking — park only
flagged the LOCAL job and a re-verify has none, and the CRM's cancel came back from progress()
and was dropped. The only way left was `stop release`, which unloaded the launchd job, and the Mac
then sat offline for two days because no CRM command can reach an agent that is not running.

Fakes only: no Playwright, no Chrome, no `data/leads.db`.
"""
from __future__ import annotations

import threading

import pytest

from webscraper import agent, wa_verify
from webscraper.store import Store


class FakeCloud:
    def __init__(self, rows, cancel_after: int | None = None):
        self.rows = rows
        self.cancel_after = cancel_after
        self.progress_calls = 0
        self.done_calls: list[tuple] = []
        self.lock = threading.Lock()

    def results(self, jid):
        return self.rows

    def progress(self, jid, phase, d):
        with self.lock:
            self.progress_calls += 1
            return self.cancel_after is not None and self.progress_calls >= self.cancel_after

    def set_wa(self, jid, updates):
        pass

    def done(self, jid, status, msg=None):
        self.done_calls.append((status, msg))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    s = Store(tmp_path / "leads.db")
    for name in ("a1", "a2", "a3"):
        s.add_wa_account(name)
    local = int(s.create_job("q", "l", 10, 1.0, country="IN"))
    s.conn.execute("UPDATE jobs SET cloud_id=1 WHERE id=?", (local,))
    s.conn.commit()
    rows = [{"place_key": f"lead-{i}", "name": f"L{i}", "number": f"+9198765432{i:02d}"} for i in range(6)]
    checked: list[str] = []
    hook: dict = {"after_first": None}

    def fake_verify(st, rows, on_progress=None, should_stop=None, job_id=None, headless=None, account=None):
        n = 0
        for r in rows:
            if should_stop and should_stop():         # the real verify_places checks per number too
                break
            on_progress(r["place_key"], "yes", r["number"].lstrip("+"), "leads")
            checked.append(r["place_key"])
            n += 1
            if n == 1 and hook["after_first"]:
                hook["after_first"]()
        return {"yes": n, "no": 0, "unknown": 0, "checked": n, "capped": 0, "no_number": 0}

    monkeypatch.setattr(wa_verify, "verify_places", fake_verify)
    monkeypatch.setattr("webscraper.lanes._wa_parallel", lambda: 1)
    yield s, rows, checked, hook
    s.close()


def _own_run(monkeypatch, jid: int = 1) -> threading.Event:
    """Pretend `_start_reverify` launched job `jid` on this (alive) thread."""
    ev = threading.Event()
    monkeypatch.setitem(agent._REVERIFY, "thread", threading.current_thread())
    monkeypatch.setitem(agent._REVERIFY, "job", jid)
    monkeypatch.setitem(agent._REVERIFY, "stop", ev)
    monkeypatch.setitem(agent._REVERIFY, "why", None)
    return ev


def test_crm_cancel_stops_the_run_and_sends_no_done(setup):
    s, rows, checked, _ = setup
    # progress() #1 is the run's opening report, then one per verdict: the CRM says "cancelled"
    # in its answer to the 2nd verdict, and the run stops there.
    cloud = FakeCloud(rows, cancel_after=3)
    agent._reverify_wa(cloud, s, 1, leads_verify=True)
    assert len(checked) == 2                                   # stopped after the number in hand
    assert cloud.done_calls == []                              # the CRM already says "cancelled"


def test_park_stops_a_running_reverify(setup, monkeypatch):
    s, rows, checked, hook = setup
    _own_run(monkeypatch)
    hook["after_first"] = lambda: agent._stop_reverify("parked")
    cloud = FakeCloud(rows)
    agent._reverify_wa(cloud, s, 1, leads_verify=True)
    assert checked == ["lead-0"]
    assert len(cloud.done_calls) == 1
    status, msg = cloud.done_calls[0]
    assert status == "error" and "parked" in msg and "5 left" in msg


def test_parallel_slices_honour_the_stop(setup, monkeypatch):
    s, rows, checked, _ = setup
    monkeypatch.setattr("webscraper.lanes._wa_parallel", lambda: 3)
    _own_run(monkeypatch).set()                                # parked before any slice started
    cloud = FakeCloud(rows)
    agent._reverify_wa(cloud, s, 1, leads_verify=True)
    assert checked == []
    assert [d[0] for d in cloud.done_calls] == ["error"]


def test_an_unstopped_run_still_finishes_done(setup, monkeypatch):
    s, rows, checked, _ = setup
    _own_run(monkeypatch)
    cloud = FakeCloud(rows)
    agent._reverify_wa(cloud, s, 1, leads_verify=True)
    assert len(checked) == len(rows) and cloud.done_calls == [("done", None)]


def test_stop_reverify_without_a_run_is_a_noop(monkeypatch):
    monkeypatch.setitem(agent._REVERIFY, "thread", None)
    monkeypatch.setitem(agent._REVERIFY, "stop", threading.Event())
    assert agent._stop_reverify("parked") is False


def test_a_mac_always_parks_even_on_release():
    assert agent._stop_parks("release", "darwin") is True     # never `launchctl bootout` again
    assert agent._stop_parks("release", "win32") is False     # Windows: free the locked folder
    assert agent._stop_parks("RELEASE ", "linux") is False
    assert agent._stop_parks(None, "win32") is True
    assert agent._stop_parks("", "darwin") is True
