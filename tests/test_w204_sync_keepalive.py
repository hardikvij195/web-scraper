"""W204 (CRM T1470, MAC 2026-10-30): a pinned WhatsApp session that keeps landing on the sync splash
keeps its browser OPEN and waits the sync out in place (no new send-URL load) instead of closing it and
leaving the job — closing restarted the sync on every job (216 checks/h, "keeps re-syncing" x4).
The keep-alive is capped; past the cap the W137 bench + W171 stuck flag apply. Also: 10-session cap.

Fakes only: no Playwright, no Chrome, no `data/leads.db`.
"""
from __future__ import annotations

from webscraper import lanes as L, wa_verify as wv
import pytest

from tests.test_w115_wa_sync_requeue import _Store, _rows, wa  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _clean_ladder():
    """The W143 re-sync ladder / W142 pause state are module-level — leave none behind for other tests."""
    yield
    wv.resync_reset("acc1")
    getattr(wv, "_SYNC_MAX_OVERRIDE", {}).pop("acc1", None)


def test_decision_waits_only_when_pinned_and_under_the_cap():
    assert wv.sync_keepalive_action(True, 0.0, 600.0) == "wait"
    assert wv.sync_keepalive_action(True, 599.0, 600.0) == "wait"
    assert wv.sync_keepalive_action(True, 600.0, 600.0) == "bench"
    assert wv.sync_keepalive_action(False, 0.0, 600.0) == "bench"   # unpinned lane run = W137 as before
    assert wv.sync_keepalive_action(True, 0.0, 0.0) == "bench"      # switched off


def test_keepalive_env_override(monkeypatch):
    monkeypatch.setenv("WA_SYNC_KEEPALIVE_SEC", "90")
    assert wv.wa_sync_keepalive_sec() == 90.0
    monkeypatch.setenv("WA_SYNC_KEEPALIVE_SEC", "junk")
    assert wv.wa_sync_keepalive_sec() == wv.WA_SYNC_KEEPALIVE_SEC


def test_wa_parallel_cap_is_10(monkeypatch):
    monkeypatch.setenv("WA_PARALLEL", "10")
    assert L._wa_parallel() == 10
    monkeypatch.setenv("WA_PARALLEL", "12")
    assert L._wa_parallel() == 10


def _fakes(monkeypatch, calls, sync_until_waits):
    class _Ctx:
        def close(self):
            calls["closed"] += 1

    class _Page:
        def goto(self, url, **kw):
            calls["goto"] += 1

    monkeypatch.setattr(wv, "_ensure_session",
                        lambda pw, open_ctx, rl, name, headless=None: open_ctx.setdefault(name, (_Ctx(), _Page()))[1])
    monkeypatch.setattr(wv, "_boot_state", lambda page: "syncing" if calls["waits"] < sync_until_waits else "chat")

    def wait_boot(page, name, **kw):
        calls["waits"] += 1
        return "syncing" if calls["waits"] < sync_until_waits else "chat"
    monkeypatch.setattr(wv, "wait_boot", wait_boot)
    monkeypatch.setattr(wv, "_decide", lambda page: "unknown" if calls["waits"] < sync_until_waits else "yes")
    monkeypatch.setattr(wv, "wa_delay_range", lambda: (0.0, 0.0))


def test_pinned_session_waits_the_sync_out_with_its_browser_open(wa, monkeypatch):
    calls = {"closed": 0, "goto": 0, "waits": 0}
    rec: list[str] = []
    stuck: list[bool] = []

    class _S(_Store):
        def record_wa_check(self, job_id, pk, e164, source, status, name):
            rec.append(status)

        def set_wa_sync_stuck(self, name, flag):
            stuck.append(flag)

    _fakes(monkeypatch, calls, sync_until_waits=12)
    monkeypatch.setattr(wv, "wa_sync_keepalive_sec", lambda: 10_000.0)
    res = wv.verify_places(_S(), _rows(5), job_id=7, account="acc1")
    assert res["checked"] == 5 and res["yes"] == 5          # every number decided once the sync finished
    assert "unknown" not in rec                             # nothing recorded off the splash
    assert True not in stuck                                # never benched as stuck
    assert not res.get("sync_blocked")


def test_pinned_session_benched_as_stuck_once_the_budget_is_spent(wa, monkeypatch):
    calls = {"closed": 0, "goto": 0, "waits": 0}
    stuck: list[bool] = []

    class _S(_Store):
        def record_wa_check(self, *a, **k):
            raise AssertionError("a syncing session must never record a verdict")

        def set_wa_sync_stuck(self, name, flag):
            stuck.append(flag)

    _fakes(monkeypatch, calls, sync_until_waits=10 ** 9)   # never finishes
    monkeypatch.setattr(wv, "wa_sync_keepalive_sec", lambda: 3.0)
    res = wv.verify_places(_S(), _rows(25), job_id=7, account="acc1")
    assert res["checked"] == 0
    assert True in stuck                                    # W171 stuck syncing after the cap
    assert calls["closed"] >= 1                             # only now is its browser closed
