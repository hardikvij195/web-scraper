"""W102 (CRM T545): every Playwright session a WhatsApp slice or the enrichment browser tier
starts is stopped on every exit path — the `[Errno 24] Too many open files` that ended job
#6619's WhatsApp lane on the Mac after ~2 h was a driver (two pipes + an event loop) that
outlived its slice.

Fakes only: no Playwright, no Chrome, no `data/leads.db`.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from webscraper import browser_fetch as bf
from webscraper import browser_recovery as br
from webscraper import fdcount, healthcheck
from webscraper import wa_verify as wv


# ── fakes for verify_places ───────────────────────────────────────────────────────────
class _Ctx:
    def __init__(self, fail_close: bool = False) -> None:
        self.closed = False
        self._fail = fail_close

    def close(self) -> None:
        self.closed = True
        if self._fail:
            raise RuntimeError("context already gone")


class _Page:
    num = None

    def goto(self, url, **kw):
        self.num = url.split("phone=")[1].split("&")[0]


class _PW:
    """Stands in for `sync_playwright()`: counts starts and stops."""
    def __init__(self, fail_stop: bool = False) -> None:
        self.started = 0
        self.stopped = 0
        self._fail_stop = fail_stop

    def start(self):
        self.started += 1
        return self

    def stop(self):
        self.stopped += 1
        if self._fail_stop:
            raise RuntimeError("driver pipe broken")


class _Conn:
    def execute(self, *a): return self
    def commit(self): pass


class _Store:
    conn = _Conn()

    def list_wa_accounts(self):
        return [{"name": "acc1", "disabled": 0}]

    def bump_wa_account(self, name, today): pass


def _rows(n: int) -> list[dict]:
    return [{"place_key": f"p{i}", "number": f"+91987654{i:04d}", "source": "maps"} for i in range(n)]


@pytest.fixture()
def wa(monkeypatch):
    pw = _PW()
    monkeypatch.setattr(wv, "sync_playwright", lambda: pw)
    monkeypatch.setattr(wv, "_decide", lambda page: "yes")
    monkeypatch.setattr(wv, "_dismiss_popup", lambda page: None)
    monkeypatch.setattr(wv.settings, "wa_delay_min", 0.0)
    monkeypatch.setattr(wv.settings, "wa_delay_max", 0.0)
    return pw


def test_slice_stops_the_driver_and_closes_contexts_when_a_launch_blows_up(wa, monkeypatch):
    """A non-WhatsApp exception out of `_ensure_session` (a launch that failed, Errno 24 itself)
    propagates — and still leaves no driver and no context behind."""
    ctxs: list[_Ctx] = []
    calls = {"n": 0}

    def ensure(pw, open_ctx, rl, name, headless=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(24, "Too many open files")    # the relaunch for number 2 fails
        ctx = _Ctx(); ctxs.append(ctx)
        open_ctx[name] = (ctx, _Page())
        return open_ctx[name][1]

    monkeypatch.setattr(wv, "_ensure_session", ensure)
    with pytest.raises(OSError):
        wv.verify_places(_Store(), _rows(2), account="acc1")
    assert wa.started == 1 and wa.stopped == 1
    assert ctxs and all(c.closed for c in ctxs)


def test_a_context_that_fails_to_close_does_not_skip_the_driver_stop(wa, monkeypatch):
    monkeypatch.setattr(wv, "_ensure_session",
                        lambda pw, open_ctx, rl, name, headless=None:
                        open_ctx.setdefault(name, (_Ctx(fail_close=True), _Page()))[1])
    res = wv.verify_places(_Store(), _rows(2), account="acc1")
    assert res["checked"] == 2
    assert wa.started == 1 and wa.stopped == 1


def test_a_driver_that_fails_to_stop_is_logged_not_raised(monkeypatch):
    pw = _PW(fail_stop=True)
    monkeypatch.setattr(wv, "sync_playwright", lambda: pw)
    monkeypatch.setattr(wv, "_decide", lambda page: "no")
    monkeypatch.setattr(wv, "_dismiss_popup", lambda page: None)
    monkeypatch.setattr(wv.settings, "wa_delay_min", 0.0)
    monkeypatch.setattr(wv.settings, "wa_delay_max", 0.0)
    monkeypatch.setattr(wv, "_ensure_session",
                        lambda pw, open_ctx, rl, name, headless=None:
                        open_ctx.setdefault(name, (_Ctx(), _Page()))[1])
    res = wv.verify_places(_Store(), _rows(1), account="acc1")
    assert res["no"] == 1 and pw.stopped == 1


def test_driver_is_not_started_before_the_targets_are_expanded(wa, monkeypatch):
    """The expansion loop writes to sqlite through the progress callback; when that raises
    (`database is locked`) there must be no driver to leak — it used to start first."""
    monkeypatch.setattr(wv, "_ensure_session", lambda *a, **k: pytest.fail("no session expected"))

    def boom(*a):
        raise RuntimeError("database is locked")
    row = {"place_key": "p0", "phone": None, "whatsapp_number": None, "site_phones": None,
           "country": "IN", "raw": None}
    with pytest.raises(RuntimeError):
        wv.verify_places(_Store(), [row], on_progress=boom, account="acc1")
    assert wa.started == 0 and wa.stopped == 0


def test_no_targets_means_no_driver_at_all(wa, monkeypatch):
    res = wv.verify_places(_Store(), [], account="acc1")
    assert res["checked"] == 0 and wa.started == 0 and wa.stopped == 0


def test_a_pinned_slice_ends_when_its_account_is_logged_out(wa, monkeypatch):
    """`account=` pins the slice; a logged-out answer used to `continue` into the same
    profile for every remaining number (a launch + 40 s wait each)."""
    calls = {"n": 0}

    def ensure(pw, open_ctx, rl, name, headless=None):
        calls["n"] += 1
        return None                                   # logged out
    monkeypatch.setattr(wv, "_ensure_session", ensure)
    res = wv.verify_places(_Store(), _rows(5), account="acc1")
    assert res["checked"] == 0
    assert calls["n"] == 1
    assert wa.started == 1 and wa.stopped == 1


def test_a_pinned_slice_ends_when_the_session_drops_mid_run(wa, monkeypatch):
    calls = {"ensure": 0, "check": 0}

    def ensure(pw, open_ctx, rl, name, headless=None):
        calls["ensure"] += 1
        return open_ctx.setdefault(name, (_Ctx(), _Page()))[1]

    def decide(page):
        calls["check"] += 1
        raise wv.WaNotLoggedIn("profile has no live WhatsApp Web session")
    monkeypatch.setattr(wv, "_ensure_session", ensure)
    monkeypatch.setattr(wv, "_decide", decide)
    res = wv.verify_places(_Store(), _rows(5), account="acc1")
    assert res["checked"] == 0 and calls["check"] == 1 and calls["ensure"] == 1
    assert wa.stopped == 1


# ── Relauncher: the dead context is closed before the new one exists ──────────────────
def test_relauncher_closes_the_old_context_before_opening_the_new_one(monkeypatch, tmp_path: Path):
    order: list[str] = []

    class Ctx:
        pages: list = []

        def close(self):
            order.append("close")

    def open_fn():
        order.append("open")
        return Ctx(), object()

    monkeypatch.setattr(br, "kill_profile_holder", lambda d, reason="": order.append("kill") or False)
    monkeypatch.setattr(br, "RELAUNCH_SETTLE_SEC", 0)
    rl = br.Relauncher(open_fn, profile_dir=tmp_path)
    first, _ = rl.open()
    assert rl.recover("number x") is True
    assert order == ["open", "close", "kill", "open"]
    assert rl.ctx is not first


# ── BrowserFetcher: a boot the caller gave up on still releases its Playwright ─────────
class _FakePW:
    made: list["_FakePW"] = []

    def __init__(self) -> None:
        self.exited = False
        _FakePW.made.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.exited = True


class _FakeCtx:
    pages: list = []

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_browser_fetch_boot_timeout_releases_the_late_browser(monkeypatch, tmp_path: Path):
    gate = threading.Event()
    ctxs: list[_FakeCtx] = []

    class Fetcher(bf.BrowserFetcher):
        def _playwright(self):
            return _FakePW()

        def _profile_dir(self):
            return tmp_path / "prof"

        def _opener(self, pw):
            def _open():
                gate.wait(5)                          # the slow launch
                c = _FakeCtx(); ctxs.append(c)
                return c, object()
            return _open

    monkeypatch.setattr(bf, "BOOT_TIMEOUT_SEC", 0.2)
    monkeypatch.setattr(bf, "reap_orphan_browsers", lambda dirs, reason="": 0)
    _FakePW.made.clear()
    with pytest.raises(RuntimeError, match="did not start"):
        Fetcher(headless=True)
    gate.set()                                        # the launch finishes after the caller left
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not (_FakePW.made and _FakePW.made[0].exited):
        time.sleep(0.02)
    assert ctxs and ctxs[0].closed, "the late context was never closed"
    assert _FakePW.made[0].exited, "the Playwright session outlived the fetcher"


# ── fdcount + healthcheck ─────────────────────────────────────────────────────────────
def test_fd_count_and_status_never_raise():
    n = fdcount.fd_count()
    assert n is None or n > 0
    assert fdcount.fd_status().startswith("fd=")
    lim = fdcount.fd_limit()
    if sys.platform == "win32":
        assert lim is None and fdcount.raise_fd_limit() is None
    else:
        assert lim is not None and len(lim) == 2
        old, new = fdcount.raise_fd_limit()
        assert new >= old


def test_healthcheck_reports_the_open_files_limit():
    c = healthcheck._fd_limit()
    assert set(c) >= {"ok", "detail", "fix"}
    assert "open-files" in c["detail"]
    assert "fd_limit" in healthcheck.run_checks()["checks"]
