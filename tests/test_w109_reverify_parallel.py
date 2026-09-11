"""W109 (CRM T576): a leads-verify / re-verify runs one WhatsApp session per linked account
at once (min(wa_parallel, accounts)), each slice on its own account and its own Store, and
every verdict still reaches the CRM exactly once."""
from __future__ import annotations

import threading

import pytest

from webscraper import agent, wa_verify
from webscraper.store import Store


class FakeCloud:
    def __init__(self, rows):
        self.rows = rows
        self.set_wa_calls: list[dict] = []
        self.done_calls: list[tuple] = []
        self.lock = threading.Lock()

    def results(self, jid):
        return self.rows

    def progress(self, jid, phase, d):
        pass

    def set_wa(self, jid, updates):
        with self.lock:
            self.set_wa_calls.extend(updates)

    def done(self, jid, status, msg=None):
        self.done_calls.append((status, msg))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    s = Store(tmp_path / "leads.db")
    for name in ("a1", "a2", "a3"):
        s.add_wa_account(name)
    # A mirrored local job for cloud job 1, so `_jlog` really writes from the slice threads.
    local = int(s.create_job("q", "l", 10, 1.0, country="IN"))
    s.conn.execute("UPDATE jobs SET cloud_id=1 WHERE id=?", (local,))
    s.conn.commit()
    rows = [{"place_key": f"lead-{i}", "name": f"L{i}", "number": f"+9198765432{i:02d}"} for i in range(7)]
    calls: list[dict] = []
    lock = threading.Lock()

    def fake_verify(st, rows, on_progress=None, should_stop=None, job_id=None, headless=None, account=None):
        with lock:
            calls.append({"account": account, "keys": [r["place_key"] for r in rows],
                          "own_store": st is not s})
        for r in rows:
            on_progress(r["place_key"], "yes", r["number"].lstrip("+"), "leads")
        return {"yes": len(rows), "no": 0, "unknown": 0, "checked": len(rows), "capped": 0, "no_number": 0}

    monkeypatch.setattr(wa_verify, "verify_places", fake_verify)
    yield s, rows, calls
    s.close()


def test_parallel_slices_cover_every_lead_once(setup, monkeypatch):
    s, rows, calls = setup
    monkeypatch.setattr("webscraper.lanes._wa_parallel", lambda: 4)   # capped by 3 accounts
    cloud = FakeCloud(rows)
    agent._reverify_wa(cloud, s, 1, leads_verify=True)

    assert sorted(c["account"] for c in calls) == ["a1", "a2", "a3"]
    assert all(c["own_store"] for c in calls)                          # never the caller's Store
    keys = [k for c in calls for k in c["keys"]]
    assert sorted(keys) == sorted(r["place_key"] for r in rows)        # all, and disjoint
    per_lead = [u for u in cloud.set_wa_calls if "wa_numbers" in u]
    assert sorted(u["place_key"] for u in per_lead) == sorted(keys)    # one live update per verdict
    assert all(u["wa_verified"] == "yes" for u in per_lead)
    assert cloud.done_calls == [("done", None)]


def test_one_session_keeps_the_rotating_path(setup, monkeypatch):
    s, rows, calls = setup
    monkeypatch.setattr("webscraper.lanes._wa_parallel", lambda: 1)
    agent._reverify_wa(FakeCloud(rows), s, 1, leads_verify=True)
    assert len(calls) == 1 and calls[0]["account"] is None and not calls[0]["own_store"]


def test_a_failed_slice_does_not_sink_the_others(setup, monkeypatch):
    s, rows, calls = setup
    monkeypatch.setattr("webscraper.lanes._wa_parallel", lambda: 2)
    real = wa_verify.verify_places

    def flaky(st, rows, on_progress=None, should_stop=None, job_id=None, headless=None, account=None):
        if account == "a1":
            raise wa_verify.WaNotLoggedIn("a1 logged out")
        return real(st, rows, on_progress, should_stop, job_id, headless, account)

    monkeypatch.setattr(wa_verify, "verify_places", flaky)
    cloud = FakeCloud(rows)
    agent._reverify_wa(cloud, s, 1, leads_verify=True)
    assert [c["account"] for c in calls] == ["a2"]
    assert cloud.done_calls == [("done", None)]
