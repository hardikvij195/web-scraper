"""W137 (CRM T1016): a WhatsApp Web session that never leaves "messages are downloading" must
not burn real numbers as 'unknown' (MAC, 2026-10-04: 34 undecided, 0 verdicts in an hour).

  * verify_places benches the account after WA_SYNC_STRIKES sync non-answers, records nothing,
    closes its browser and reports `sync_blocked`
  * a re-check straight off the sync splash that still cannot decide is a sync non-answer too
  * the WhatsApp lane parks (slot released) on `sync_blocked`; W143 moved the escalation to a
    per-account ladder (tests/test_w143_resync_failsafe.py)
  * `restart` with arg `now` is never deferred (the CRM's frozen-machine backstop)

Fakes only: no Playwright, no Chrome, no `data/leads.db`.
"""
from __future__ import annotations

import time

from webscraper import agent, lanes as L, wa_verify as wv
from tests.test_w115_wa_sync_requeue import _Store, _rows, wa  # noqa: F401  (fixture)


def test_sync_strikes_bench_the_account_and_record_nothing(wa, monkeypatch):
    calls = {"decide": 0, "closed": 0}
    logs: list[str] = []

    class _S(_Store):
        def log(self, job_id, lane, message, level="info"):
            logs.append(message)

        def record_wa_check(self, *a, **k):
            raise AssertionError("a syncing session must never record a verdict")

    class _Ctx:
        def close(self):
            calls["closed"] += 1

    class _Page:
        def goto(self, url, **kw):
            pass

    monkeypatch.setattr(wv, "_ensure_session",
                        lambda pw, open_ctx, rl, name, headless=None: open_ctx.setdefault(name, (_Ctx(), _Page()))[1])

    def decide(page):
        calls["decide"] += 1
        return "unknown"
    monkeypatch.setattr(wv, "_decide", decide)
    monkeypatch.setattr(wv, "_boot_state", lambda page: "syncing")
    monkeypatch.setattr(wv, "wait_boot", lambda page, name, **kw: "syncing")

    res = wv.verify_places(_S(), _rows(25), job_id=7)
    assert res["sync_blocked"] is True
    assert res["checked"] == 0 and res["unknown"] == 0
    assert calls["decide"] == wv.WA_SYNC_STRIKES          # three tries, then benched — not 25 x 3
    assert calls["closed"] >= 1                           # its browser was closed for a clean boot
    assert any("keeps re-syncing" in m for m in logs)


def test_recheck_off_the_splash_that_cannot_decide_is_not_an_answer(wa, monkeypatch):
    """boot_state says syncing, the bounded wait ends on 'chat', the re-check still says
    unknown: before W137 that 'unknown' was recorded; now it is a sync non-answer."""
    class _S(_Store):
        recorded: list = []

        def record_wa_check(self, job_id, pk, num, source, status, name=None):
            self.recorded.append(status)

    monkeypatch.setattr(wv, "_decide", lambda page: "unknown")
    monkeypatch.setattr(wv, "_boot_state", lambda page: "syncing")
    monkeypatch.setattr(wv, "wait_boot", lambda page, name, **kw: "chat")
    res = wv.verify_places(_S(), _rows(3), job_id=7)
    assert res["sync_blocked"] is True and _S.recorded == []


def test_a_real_verdict_clears_the_strikes(wa, monkeypatch):
    seq = iter(["unknown", "unknown", "yes", "yes"])      # two strikes, then real answers
    monkeypatch.setattr(wv, "_decide", lambda page: next(seq, "yes"))
    monkeypatch.setattr(wv, "_boot_state", lambda page: "syncing")
    monkeypatch.setattr(wv, "wait_boot", lambda page, name, **kw: "syncing")
    res = wv.verify_places(_Store(), _rows(2))
    assert res["sync_blocked"] is False
    assert res["yes"] == 2 and res["unknown"] == 0


# W143: the lane-park give-up test moved to tests/test_w143_resync_failsafe.py (per-account ladder).


def test_restart_now_is_never_deferred():
    assert agent._should_defer({"command": "restart", "arg": "now"}, 53) is False
    assert agent._should_defer({"command": "restart", "arg": ""}, 53) is True
    assert agent._should_defer({"command": "restart"}, 53) is True
    assert agent._should_defer({"command": "update", "arg": "now"}, 53) is True
