"""W115 (CRM T646): a number checked while WhatsApp Web was still syncing must not be
recorded 'unknown' — it is re-queued (bounded by WA_REQUEUE_MAX) and only actually decided
once the client is ready, or after the run gave it every chance.

Before this, `_check`'s one reactive retry (W96) still recorded whatever `_decide` said even
when the client was demonstrably still on its sync splash — one account answered 'unknown' on
13 of 42 numbers that way. `verify_places` now also pauses the whole batch when it finds an
account mid-sync BEFORE walking into the next number (the proactive gate), and `_check`
returns a sentinel instead of 'unknown' when its own wait still ends mid-sync, so the caller
puts the number back on the queue.

Fakes only: no Playwright, no Chrome, no `data/leads.db`.
"""
from __future__ import annotations

import pytest

from webscraper import wa_verify as wv


class _Ctx:
    def close(self) -> None:
        pass


class _Page:
    def goto(self, url, **kw):
        pass


class _PW:
    def start(self):
        return self

    def stop(self):
        pass


class _Conn:
    def execute(self, *a):
        return self

    def commit(self):
        pass


class _Store:
    conn = _Conn()

    def list_wa_accounts(self):
        return [{"name": "acc1", "disabled": 0}]

    def pick_wa_account(self, cap, today, exclude=()):
        return "acc1" if "acc1" not in (exclude or ()) else None

    def bump_wa_account(self, name, today):
        pass

    def log(self, *a, **kw):
        pass


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
                        lambda pw, open_ctx, rl, name, headless=None: open_ctx.setdefault(name, (_Ctx(), _Page()))[1])
    return pw


def test_still_syncing_is_requeued_not_recorded_unknown(wa, monkeypatch):
    """The client never comes back this run — every attempt (the first check plus every
    re-queue) sees 'syncing', so the number is eventually recorded 'unknown' only after
    WA_REQUEUE_MAX re-queues, never on the first pass."""
    calls = {"decide": 0}

    def decide(page):
        calls["decide"] += 1
        return "unknown"

    monkeypatch.setattr(wv, "_decide", decide)
    monkeypatch.setattr(wv, "_boot_state", lambda page: "syncing")
    monkeypatch.setattr(wv, "wait_boot", lambda page, name, **kw: "syncing")

    res = wv.verify_places(_Store(), _rows(1))

    assert res["checked"] == 1
    assert res["unknown"] == 1
    # 1 initial attempt + WA_REQUEUE_MAX re-queued attempts, never unbounded.
    assert calls["decide"] == 1 + wv.WA_REQUEUE_MAX


def test_still_syncing_recovers_once_the_client_settles(wa, monkeypatch):
    """A requeue that lands after the sync finishes gets the real verdict — proving the
    number was genuinely retried, not just discarded and re-labelled. The account is mid-sync
    the whole run (boot_state/wait_boot always say so — a real sync often outlasts one number's
    checks); what changes is `_decide` itself: unresolvable on the first attempt (the send URL
    loaded before the client caught up), a clean 'yes' on the retried attempt."""
    state = {"n": 0}

    def decide(page):
        state["n"] += 1
        return "unknown" if state["n"] == 1 else "yes"

    monkeypatch.setattr(wv, "_decide", decide)
    monkeypatch.setattr(wv, "_boot_state", lambda page: "syncing")
    monkeypatch.setattr(wv, "wait_boot", lambda page, name, **kw: "syncing")

    res = wv.verify_places(_Store(), _rows(1))

    assert res["checked"] == 1
    assert res["yes"] == 1
    assert res["unknown"] == 0
    assert state["n"] == 2   # the first (unresolved) attempt, then the re-queued one
