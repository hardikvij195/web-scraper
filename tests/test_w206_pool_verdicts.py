"""W206 (CRM T1476): a re-verify / pool job's WhatsApp verdicts all land, and a first 'no' gets its
second look in the same run (the CRM holds a first 'no' at NULL — trigger lead_gen_results_wa_no_2x).

MAC #23346 (2026-10-30): 5,086 checks reported, 2,288 rows still NULL — every one a 'no_once'.
Fakes only: no Playwright, no Chrome.
"""
from __future__ import annotations

import httpx
import pytest

from webscraper import agent, wa_verify
from webscraper.store import Store


class FakeCloud:
    def __init__(self, rows, progress_fails: int = 0):
        self.rows = rows
        self.sent: list[dict] = []
        self.calls: list[int] = []            # rows per set_wa call
        self.progress_calls: list[dict] = []
        self.progress_fails = progress_fails
        self.done_calls: list[tuple] = []

    def results(self, jid):
        return self.rows

    def progress(self, jid, phase, d):
        self.progress_calls.append(d)
        if self.progress_fails and len(self.progress_calls) > 1:   # the opening report goes through
            self.progress_fails -= 1
            raise httpx.ConnectError("progress blip")
        return False

    def set_wa(self, jid, updates):
        self.calls.append(len(updates))
        self.sent.extend(updates)

    def logs(self, jid, rows):
        pass

    def done(self, jid, status, msg=None):
        self.done_calls.append((status, msg))


def _rows(n, hist=None):
    return [{"place_key": f"r:{100 + i}", "name": f"L{i}", "phone": f"+6129876543{i}", "country": "AU",
             "whatsapp_source": None, "wa_verified": None, "wa_numbers": hist(i) if hist else None}
            for i in range(n)]


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = Store(tmp_path / "leads.db")
    s.add_wa_account("a1")
    monkeypatch.setattr("webscraper.lanes._wa_parallel", lambda: 1)
    monkeypatch.setattr(agent.time, "sleep", lambda *_: None)
    monkeypatch.setattr(wa_verify.POOL, "close_idle", lambda *a, **k: None)
    yield s
    s.close()


def _fake_verify(passes: list[list[dict]], verdict_for):
    def fake(st, targets, on_progress=None, should_stop=None, job_id=None, **_kw):
        passes.append(list(targets))
        for r in targets:
            num = (r.get("number") or r["phone"]).lstrip("+")
            on_progress(r["place_key"], verdict_for(r, len(passes)), num, r.get("source") or "maps")
        return {"yes": 0, "no": len(targets), "checked": len(targets)}
    return fake


def test_first_no_gets_a_second_look_in_the_same_run(store, monkeypatch):
    rows = _rows(4)
    passes: list[list[dict]] = []
    # r:100 = yes; the rest 'no' both times
    monkeypatch.setattr(wa_verify, "verify_places",
                        _fake_verify(passes, lambda r, p: "yes" if r["place_key"] == "r:100" else "no"))
    cloud = FakeCloud(rows)
    agent._reverify_wa(cloud, store, 1)
    assert len(passes) == 2
    assert sorted(r["place_key"] for r in passes[1]) == ["r:101", "r:102", "r:103"]
    assert all(r["number"].startswith("+61") for r in passes[1])
    # one live set_wa per check, each with its wa_numbers delta (checks: 1) — no bulk re-send
    assert cloud.calls == [1] * 7
    assert all(u.get("wa_numbers") for u in cloud.sent)
    last = cloud.progress_calls[-1]
    assert last["wa_verify_total"] == 7 and last["wa_verify_done"] == 7
    assert cloud.done_calls[-1][0] == "done"


def test_no_second_look_when_the_crm_already_had_one_no(store, monkeypatch):
    rows = _rows(2, hist=lambda i: [{"number": f"+6129876543{i}", "verdict": "no_once", "checks": 1}])
    passes: list[list[dict]] = []
    monkeypatch.setattr(wa_verify, "verify_places", _fake_verify(passes, lambda r, p: "no"))
    agent._reverify_wa(FakeCloud(rows), store, 1)
    assert len(passes) == 1                 # this run's 'no' is look #2 -> the CRM settles it


def test_unknown_is_not_second_looked(store, monkeypatch):
    passes: list[list[dict]] = []
    monkeypatch.setattr(wa_verify, "verify_places", _fake_verify(passes, lambda r, p: "unknown"))
    agent._reverify_wa(FakeCloud(_rows(3)), store, 1)
    assert len(passes) == 1


def test_failed_progress_never_skips_the_verdict_write(store, monkeypatch):
    rows = _rows(3)
    passes: list[list[dict]] = []
    monkeypatch.setattr(wa_verify, "verify_places", _fake_verify(passes, lambda r, p: "yes"))
    cloud = FakeCloud(rows, progress_fails=3)
    agent._reverify_wa(cloud, store, 1)
    assert {u["place_key"] for u in cloud.sent} == {"r:100", "r:101", "r:102"}
    assert all(u.get("wa_numbers") for u in cloud.sent)


def test_second_look_targets_rules():
    rows = {"a": {"place_key": "a", "wa_numbers": None},
            "b": {"place_key": "b", "wa_numbers": [{"number": "+61200000001", "verdict": "no_once", "checks": 1}]},
            "c": {"place_key": "c", "wa_numbers": None}}
    checks = {"a": [{"number": "+61200000000", "verdict": "no", "source": "maps"}],
              "b": [{"number": "+61200000001", "verdict": "no", "source": "maps"}],
              "c": [{"number": "+61200000002", "verdict": "no"}, {"number": "+61400000002", "verdict": "yes"}]}
    out = agent.second_look_targets(checks, rows)
    assert [(r["place_key"], r["number"]) for r in out] == [("a", "+61200000000")]
