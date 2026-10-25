"""W195 (CRM T1387): every WhatsApp session of a job pulls from ONE shared queue (`WaFeed`), so no
account idles while another still grinds its share; an account linked mid-job (or allowed by a raised
`wa_parallel`) joins the running re-verify; numbers a session took but could not check go back."""
from __future__ import annotations

import threading
import time

import pytest

from webscraper import agent, wa_verify
from webscraper.store import Store


def _rows(n: int) -> list[dict]:
    return [{"place_key": f"p{i}", "number": f"+9198765{i:05d}", "source": "maps"} for i in range(n)]


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "leads.db")
    yield s
    s.close()


def _fake_verify(per_account_sec: dict[str, float], seen: dict[str, list[str]], lock: threading.Lock):
    def fake(st, rows, on_progress=None, should_stop=None, job_id=None, headless=None, account=None):
        n = 0
        for r in rows:                                    # drains the shared WaFeed row by row
            time.sleep(per_account_sec.get(account, 0.0))
            with lock:
                seen.setdefault(account, []).append(r["place_key"])
            on_progress(r["place_key"], "yes", r["number"].lstrip("+"), "maps")
            n += 1
        return {"yes": n, "no": 0, "unknown": 0, "checked": n, "capped": 0, "no_number": 0}
    return fake


def _all_keys(seen: dict[str, list[str]]) -> list[str]:
    return sorted(k for v in seen.values() for k in v)


def test_feed_is_shared_and_drained_once():
    feed = wa_verify.WaFeed(_rows(5))
    assert len(feed) == 5
    a = feed.take()
    feed.put_back([a])
    assert [r["place_key"] for r in feed] == ["p0", "p1", "p2", "p3", "p4"]
    assert feed.take() is None and len(feed) == 0


def test_fast_account_keeps_working_instead_of_idling(store, monkeypatch):
    for a in ("fast", "slow"):
        store.add_wa_account(a)
    seen: dict[str, list[str]] = {}
    monkeypatch.setattr(wa_verify, "verify_places",
                        _fake_verify({"fast": 0.002, "slow": 0.05}, seen, threading.Lock()))
    rows = _rows(40)
    got: list[str] = []
    res = agent._verify_slices(store, rows, ["fast", "slow"], lambda pk, *a, **k: got.append(pk))
    assert _all_keys(seen) == sorted(r["place_key"] for r in rows)       # every number exactly once
    assert len(seen["fast"]) > 2 * len(seen.get("slow", []))             # the fast one took the rest
    assert res["checked"] == 40 and res["dropped"] == 0


def test_account_linked_mid_job_joins_the_running_queue(store, monkeypatch):
    store.add_wa_account("a1")
    monkeypatch.setattr(agent, "WA_JOIN_POLL_SEC", 0.02)
    monkeypatch.setattr("webscraper.lanes._wa_parallel", lambda: 3)
    monkeypatch.setattr(agent, "_join_memory_ok", lambda: True)
    seen: dict[str, list[str]] = {}
    monkeypatch.setattr(wa_verify, "verify_places",
                        _fake_verify({"a1": 0.02, "a2": 0.02}, seen, threading.Lock()))
    threading.Timer(0.1, lambda: Store(store.path).add_wa_account("a2")).start()   # linked mid-job
    rows = _rows(60)
    agent._verify_slices(store, rows, ["a1"], lambda *a, **k: None)
    assert seen.get("a2"), "the newly linked account never joined"
    assert _all_keys(seen) == sorted(r["place_key"] for r in rows)


def test_join_respects_the_parallel_limit_and_memory_guard(store, monkeypatch):
    for a in ("a1", "a2", "a3"):
        store.add_wa_account(a)
    monkeypatch.setattr(agent, "WA_JOIN_POLL_SEC", 0.02)
    seen: dict[str, list[str]] = {}
    monkeypatch.setattr(wa_verify, "verify_places", _fake_verify({"a1": 0.01, "a2": 0.01}, seen, threading.Lock()))
    monkeypatch.setattr("webscraper.lanes._wa_parallel", lambda: 2)       # 2 sessions busy = no room
    monkeypatch.setattr(agent, "_join_memory_ok", lambda: True)
    agent._verify_slices(store, _rows(20), ["a1", "a2"], lambda *a, **k: None)
    assert "a3" not in seen
    seen.clear()
    monkeypatch.setattr("webscraper.lanes._wa_parallel", lambda: 3)       # room, but RAM over the limit
    monkeypatch.setattr(agent, "_join_memory_ok", lambda: False)
    agent._verify_slices(store, _rows(20), ["a1", "a2"], lambda *a, **k: None)
    assert "a3" not in seen


# -- the real verify_places loop on a shared feed (no Playwright, no WhatsApp) -------------------------

class _FakePage:
    def goto(self, *a, **k):
        pass


class _FakeCtx:
    def close(self):
        pass


class _FakePW:
    def start(self):
        return self

    def stop(self):
        pass


@pytest.fixture
def real_loop(store, monkeypatch):
    monkeypatch.setattr(wa_verify, "_wa_off", lambda: False)
    monkeypatch.setattr(wa_verify, "sync_playwright", lambda: _FakePW())
    monkeypatch.setattr(wa_verify, "_boot_state", lambda page: "chat")
    monkeypatch.setattr(wa_verify, "_decide", lambda page, timeout_ms=None: "yes")
    monkeypatch.setattr(wa_verify, "_dismiss_popup", lambda page: None)
    monkeypatch.setattr(wa_verify, "wa_delay_range", lambda: (0.0, 0.0))
    monkeypatch.setattr(wa_verify, "wa_relaunch_every", lambda: 0)
    return store


def test_real_loop_two_sessions_share_one_feed(real_loop, monkeypatch):
    for a in ("a1", "a2"):
        real_loop.add_wa_account(a)

    def ensure(pw, open_ctx, relaunchers, name, headless):
        time.sleep(0.001)
        open_ctx[name] = (_FakeCtx(), _FakePage())
        return open_ctx[name][1]

    monkeypatch.setattr(wa_verify, "_ensure_session", ensure)
    feed = wa_verify.WaFeed(_rows(30))
    seen: list[tuple[str, str]] = []
    lock = threading.Lock()
    out: list[dict] = []

    def run(name):
        st = Store(real_loop.path)
        try:
            out.append(wa_verify.verify_places(
                st, feed, on_progress=lambda pk, s, n=None, src=None: (lock.acquire(), seen.append((pk, s)),
                                                                        lock.release()),
                account=name))
        finally:
            st.close()

    ts = [threading.Thread(target=run, args=(a,)) for a in ("a1", "a2")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(pk for pk, _ in seen) == sorted(f"p{i}" for i in range(30))
    assert sum(r["checked"] for r in out) == 30 and len(feed) == 0


def test_real_loop_hands_unchecked_numbers_back(real_loop, monkeypatch):
    real_loop.add_wa_account("a1")

    def never_renders(pw, open_ctx, relaunchers, name, headless):
        raise wa_verify.WaUnavailable(f"{name}: never rendered")

    monkeypatch.setattr(wa_verify, "_ensure_session", never_renders)
    feed = wa_verify.WaFeed(_rows(4))
    res = wa_verify.verify_places(real_loop, feed, account="a1")
    assert res["checked"] == 0
    back = list(feed)
    assert [r["place_key"] for r in back] == ["p0", "p1", "p2", "p3"]   # nothing lost, order kept
    assert all(r["number"].startswith("+") for r in back)


# -- W195 speed helpers -----------------------------------------------------------------------------

def test_recycle_deferred_by_memory():
    assert wa_verify.recycle_deferred(150, 150, 0.30, 60.0)            # small Chrome: wait
    assert not wa_verify.recycle_deferred(150, 150, 0.75, 60.0)        # big: recycle
    assert not wa_verify.recycle_deferred(150, 150, None, 60.0)        # unknown (macOS): count rule
    assert not wa_verify.recycle_deferred(600, 150, 0.10, 60.0)        # hard ceiling 4 x N
    assert not wa_verify.recycle_deferred(150, 150, 0.10, 0.0)         # switched off
    assert not wa_verify.recycle_deferred(150, 0, 0.10, 60.0)          # recycle off anyway


def test_splash_label_only_says_downloading_when_whatsapp_does():
    wa_verify._BOOT_TXT.txt = "loading your chats  end-to-end encrypted"
    assert wa_verify._splash_label() == "loading"
    wa_verify._BOOT_TXT.txt = "don't close this window. your messages are downloading."
    assert "messages are downloading" in wa_verify._splash_label()
