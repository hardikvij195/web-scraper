"""W191 (CRM T1260): a re-verify keeps every checked verdict when WhatsApp drops mid-run.

Owner 2026-10-16: "if 50 numbers are checked and then WhatsApp disconnects, make sure we save the
info of those 50." Fakes only: no Playwright, no Chrome.
"""
from __future__ import annotations

import httpx
import pytest

from webscraper import agent, wa_verify
from webscraper.store import Store


class FakeCloud:
    def __init__(self, rows, fail_set_wa_on: set[str] | None = None):
        self.rows = rows
        self.fail_on = set(fail_set_wa_on or ())
        self.sent: list[dict] = []          # every set_wa row that reached the CRM
        self.progress_calls: list[dict] = []
        self.done_calls: list[tuple] = []

    def results(self, jid):
        return self.rows

    def progress(self, jid, phase, d):
        self.progress_calls.append(d)
        return False

    def set_wa(self, jid, updates):
        if len(updates) == 1 and updates[0]["place_key"] in self.fail_on:
            self.fail_on.discard(updates[0]["place_key"])     # fails once, live
            raise httpx.ConnectError("blip")
        self.sent.extend(updates)

    def logs(self, jid, rows):
        pass

    def done(self, jid, status, msg=None):
        self.done_calls.append((status, msg))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    s = Store(tmp_path / "leads.db")
    s.add_wa_account("a1")
    rows = [{"place_key": f"r:{100 + i}", "name": f"L{i}", "phone": f"+9198765432{i:02d}",
             "whatsapp_source": "unverified", "wa_verified": None} for i in range(6)]

    def dies_after_3(st, targets, on_progress=None, should_stop=None, job_id=None, headless=None, account=None):
        for r in targets[:3]:
            on_progress(r["place_key"], "yes", r["phone"].lstrip("+"), "maps")
        raise wa_verify.WaNotLoggedIn("session ended mid-run")

    monkeypatch.setattr(wa_verify, "verify_places", dies_after_3)
    monkeypatch.setattr("webscraper.lanes._wa_parallel", lambda: 1)
    monkeypatch.setattr(agent.time, "sleep", lambda *_: None)
    yield s, rows
    s.close()


def test_session_lost_keeps_checked_verdicts_and_ends_incomplete(setup):
    s, rows = setup
    cloud = FakeCloud(rows, fail_set_wa_on={"r:101"})        # 2nd verdict's live flush fails
    agent._reverify_wa(cloud, s, 1)
    by_pk = {}
    for u in cloud.sent:
        by_pk.setdefault(u["place_key"], []).append(u)
    # every checked verdict reached set_wa as a FULL row (number + history), incl. the failed one
    assert set(by_pk) == {"r:100", "r:101", "r:102"}
    for pk in ("r:100", "r:101", "r:102"):
        full = [u for u in by_pk[pk] if "wa_numbers" in u]
        assert len(full) == 1, pk                           # sent once: checks counted once
        assert full[0]["wa_verified"] == "yes" and full[0]["whatsapp_number"]
    # the 3 unchecked numbers were never written — no 'no' for a dead session
    assert not any(u["place_key"] in ("r:103", "r:104", "r:105") for u in cloud.sent)
    # lane ends wa_not_logged_in with the rest pending -> CRM marks the job Incomplete
    wa = [l for l in cloud.progress_calls[-1]["lanes"] if l["key"] == "whatsapp"][0]
    assert wa["reason"] == "wa_not_logged_in" and wa["status"] == "stopped" and wa["pending"] == 3
    assert cloud.done_calls and cloud.done_calls[-1][0] == "done"
    assert "3 left" in cloud.done_calls[-1][1]
