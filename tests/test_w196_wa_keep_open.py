"""W196 / W197 (CRM T1388): the live WhatsApp lane keeps its WhatsApp browsers open across its 25-number
batches (no WhatsApp Web boot per batch), the W195 recycle deferral works off Windows from the Chrome
tree's RSS, and WhatsApp Chrome gets a bigger V8 heap + a clear "tab crashed" log. Fakes only — no
real WhatsApp, no browser."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest

from webscraper import browser_recovery as br, chrome_args, lanes as L, wa_verify
from webscraper.store import Store, now_iso


# -- verify_places with a WaSession: opens once, kept, closed by the owner ----------------------------

class FakeCtx:
    def __init__(self, log):
        self.log, self.closed = log, False

    def close(self):
        self.closed = True
        self.log.append("ctx.close")


class FakePage:
    def __init__(self, crash_first=False):
        self.crash_first, self.gotos = crash_first, 0

    def goto(self, url, **kw):
        self.gotos += 1
        if self.crash_first:
            self.crash_first = False
            raise wa_verify.PWError("Page.goto: Page crashed")


class FakePW:
    def __init__(self, log):
        self.log = log

    def start(self):
        self.log.append("pw.start")
        return self

    def stop(self):
        self.log.append("pw.stop")


class FakeRelauncher:
    def __init__(self, log):
        self.log, self.attempts = log, 0
        self.current = (FakeCtx(log), FakePage())

    def recover(self, where):
        self.attempts += 1
        self.log.append(f"recover:{where}")
        self.current = (FakeCtx(self.log), FakePage())
        return True


@pytest.fixture
def harness(tmp_path, monkeypatch):
    log: list[str] = []
    st = Store(tmp_path / "leads.db")
    st.add_wa_account("a1")
    opts = {"crash_first": False}
    monkeypatch.setattr(wa_verify, "_wa_off", lambda: False)
    monkeypatch.setattr(wa_verify, "login_in_progress", lambda name=None: False)
    monkeypatch.setattr(wa_verify, "sync_playwright", lambda: FakePW(log))
    monkeypatch.setattr(wa_verify, "_decide", lambda page, timeout_ms=None: "yes")
    monkeypatch.setattr(wa_verify, "_boot_state", lambda page: "chat")
    monkeypatch.setattr(wa_verify, "_dismiss_popup", lambda page: None)
    monkeypatch.setattr(wa_verify, "wa_delay_range", lambda: (0.0, 0.0))
    monkeypatch.setattr(wa_verify, "wa_relaunch_every", lambda: 0)

    def ensure(pw, open_ctx, relaunchers, name, headless=None):
        if name in open_ctx:
            return open_ctx[name][1]
        log.append(f"open:{name}")
        ctx = FakeCtx(log)
        page = FakePage(crash_first=opts["crash_first"])
        open_ctx[name] = (ctx, page)
        if relaunchers is not None:
            relaunchers[name] = FakeRelauncher(log)
        return page
    monkeypatch.setattr(wa_verify, "_ensure_session", ensure)
    yield st, log, opts
    st.close()


def _rows(n, start=0):
    return [{"place_key": f"p{i}", "number": f"+9198765{i:05d}", "source": "maps"} for i in range(start, start + n)]


def test_session_keeps_the_browser_open_across_batches(harness):
    st, log, _ = harness
    sess = wa_verify.WaSession()
    for b in range(3):
        res = wa_verify.verify_places(st, _rows(5, b * 5), account="a1", session=sess)
        assert res["yes"] == 5
    assert log.count("open:a1") == 1, log                 # one WhatsApp Web boot for three batches
    assert "ctx.close" not in log and "pw.stop" not in log and log.count("pw.start") == 1
    assert "a1" in sess.open_ctx and sess.pw is not None
    sess.close()
    assert log[-2:] == ["ctx.close", "pw.stop"] and not sess.open_ctx and sess.pw is None


def test_without_a_session_everything_closes_per_call(harness):
    st, log, _ = harness
    wa_verify.verify_places(st, _rows(3), account="a1")
    wa_verify.verify_places(st, _rows(3, 3), account="a1")
    assert log.count("open:a1") == 2 and log.count("ctx.close") == 2 and log.count("pw.stop") == 2


def test_stop_or_error_closes_the_kept_session(harness):
    st, log, _ = harness
    sess = wa_verify.WaSession()
    wa_verify.verify_places(st, _rows(2), account="a1", session=sess)
    wa_verify.verify_places(st, _rows(2, 2), account="a1", should_stop=lambda: False, session=sess)
    assert "ctx.close" not in log
    calls = {"n": 0}

    def stop_after_first():
        calls["n"] += 1
        return calls["n"] > 1
    wa_verify.verify_places(st, _rows(3, 4), account="a1", should_stop=stop_after_first, session=sess)
    assert "ctx.close" in log and "pw.stop" in log and not sess.open_ctx


def test_account_out_of_rotation_is_closed_before_the_next_batch(harness):
    st, log, _ = harness
    sess = wa_verify.WaSession()
    wa_verify.verify_places(st, _rows(2), account="a1", session=sess)
    st.add_wa_account("a2")
    st.set_wa_needs_relink("a1", True)                    # a1 left the rotation between batches
    wa_verify.verify_places(st, _rows(2, 2), account="a2", session=sess)
    assert "a1" not in sess.open_ctx and "a2" in sess.open_ctx
    assert log.count("ctx.close") == 1
    sess.close()


def test_session_from_another_thread_is_not_reused(harness):
    st, log, _ = harness
    sess = wa_verify.WaSession()
    sess.owner = -1                                       # pretend another thread owns it
    wa_verify.verify_places(st, _rows(2), account="a1", session=sess)
    assert sess.owner == -1 and not sess.open_ctx and log.count("ctx.close") == 1


# -- W197: renderer crash is detected, logged, relaunched ----------------------------------------------

def test_tab_crash_is_logged_and_relaunched(harness, caplog):
    st, log, opts = harness
    opts["crash_first"] = True
    with caplog.at_level(logging.WARNING, logger=wa_verify.log.name):
        res = wa_verify.verify_places(st, _rows(1), account="a1")
    assert res["yes"] == 1
    assert any(x.startswith("recover:") for x in log), log
    assert any("WhatsApp tab crashed (out of memory?)" in r.getMessage() for r in caplog.records)


def test_is_crashed_vs_is_closed():
    e = Exception("Page.goto: Navigation failed because page crashed!")
    assert wa_verify.is_crashed(e) and not br.is_closed(e)
    assert not wa_verify.is_crashed(Exception("net::ERR_CONNECTION_RESET"))


def test_wa_heap_default_raised_maps_unchanged(monkeypatch):
    for k in ("CHROME_JS_HEAP_MB", "CHROME_JS_HEAP_MB__WA", "CHROME_JS_HEAP_MB__MAPS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(chrome_args, "device_upper", lambda: "2 - MAC")
    assert chrome_args.js_heap_mb("wa") == 1536
    assert chrome_args.js_heap_mb("maps") == 256 and chrome_args.js_heap_mb("enrich") == 256
    monkeypatch.setenv("CHROME_JS_HEAP_MB__WA__2 - MAC", "1024")
    assert chrome_args.js_heap_mb("wa") == 1024
    assert chrome_args.js_heap_mb("maps") == 256


# -- W196: off-Windows memory deferral ------------------------------------------------------------------

def test_tree_rss_counts_the_chrome_and_its_children(tmp_path):
    pdir = tmp_path / "wa-profiles" / "a1"
    procs = [
        (10, 1, 300_000, f"/Applications/Google Chrome --user-data-dir={pdir} --flag"),
        (11, 10, 200_000, "Google Chrome Helper (Renderer) --type=renderer"),
        (12, 11, 100_000, "Google Chrome Helper (GPU)"),
        (20, 1, 999_999, f"/Applications/Google Chrome --user-data-dir={pdir}-2"),   # other profile
        (21, 1, 50_000, "python"),
    ]
    assert br.tree_rss_mb(pdir, procs) == pytest.approx(600_000 / 1024)
    assert br.tree_rss_mb(tmp_path / "nope", procs) is None


def test_mac_ratio_from_rss_and_deferral(monkeypatch):
    import webscraper.agent as agent
    monkeypatch.setattr(agent, "DEVICE_NAME", "2 - MAC")
    monkeypatch.delenv("WA_RECYCLE_MEM_MB", raising=False)
    monkeypatch.delenv("WA_RECYCLE_MEM_MB__2 - MAC", raising=False)
    assert wa_verify.wa_recycle_mem_mb() == 1500
    r = wa_verify.account_mem_ratio("a1", platform="darwin", rss_mb=lambda p: 600.0)
    assert r == pytest.approx(0.4)
    assert wa_verify.recycle_deferred(150, 150, r, 60.0)                 # small Chrome: deferred
    assert not wa_verify.recycle_deferred(600, 150, r, 60.0)             # hard ceiling 4 x N
    big = wa_verify.account_mem_ratio("a1", platform="darwin", rss_mb=lambda p: 1200.0)
    assert not wa_verify.recycle_deferred(150, 150, big, 60.0)           # 80 % of the cap: recycle
    monkeypatch.setenv("WA_RECYCLE_MEM_MB__2 - MAC", "3000")
    assert wa_verify.account_mem_ratio("a1", platform="darwin", rss_mb=lambda p: 1200.0) == pytest.approx(0.4)
    monkeypatch.setenv("WA_RECYCLE_MEM_MB__2 - MAC", "0")
    assert wa_verify.account_mem_ratio("a1", platform="darwin", rss_mb=lambda p: 1.0) is None
    assert wa_verify.account_mem_ratio("a1", platform="darwin", rss_mb=lambda p: None) is None


def test_windows_still_uses_the_job_object(monkeypatch):
    monkeypatch.setattr(br, "profile_mem_ratio", lambda p: 0.25)
    assert wa_verify.account_mem_ratio("a1", platform="win32", rss_mb=lambda p: 99999.0) == 0.25


# -- W196: the live lane reuses its sessions across batches and closes them at the end -----------------

@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "test.db"
    return lambda: Store(path)


class RecSession:
    made: list["RecSession"] = []

    def __init__(self):
        self.closed, self.open_ctx, self.owner, self.pw = 0, {"x": 1}, None, None
        RecSession.made.append(self)

    def close(self):
        self.closed += 1


def _lane_setup(db, monkeypatch, accounts, batches):
    job_id = None
    s = db()
    job_id = s.create_job(query="q", location="here", max_places=10, delay_sec=0)
    for a in accounts:
        s.add_wa_account(a)
    s.conn.execute("INSERT INTO places(job_id, place_key, name, phone, enrich_status, scraped_at) "
                   "VALUES (?,?,?,?, 'done', ?)", (job_id, "p0", "biz", "+919999999999", now_iso()))
    s.conn.commit()
    s.close()
    left = {"n": batches}
    monkeypatch.setattr(Store, "pending_wa_verify",
                        lambda self, jid, limit=25: _rows(4, 100 * left["n"]) if left["n"] > 0 else [])
    monkeypatch.setattr(Store, "count_wa_pending", lambda self, jid: 0)
    monkeypatch.setattr(wa_verify, "login_in_progress", lambda name=None: False)
    monkeypatch.setattr(L, "IDLE_POLL_SEC", 0.01)
    monkeypatch.setattr(wa_verify, "WaSession", RecSession)
    RecSession.made = []
    seen: list[tuple] = []

    def fake_verify(store, batch, on_progress, should_stop, job_id=None, headless=None, account=None,
                    session=None):
        n = 0
        for r in batch:
            on_progress(r["place_key"], "yes", r["number"], "maps")
            n += 1
        seen.append((account, id(session), threading.get_ident()))
        if account in (None, accounts[0]):
            left["n"] -= 1
        return {"yes": n, "no": 0, "unknown": 0}
    monkeypatch.setattr(wa_verify, "verify_places", fake_verify)
    job = {"do_enrich": 0, "do_research": 0, "do_wa_verify": 1, "country": "IN"}
    pipe = L.Pipeline(job_id, job, lambda lane: L.R_COMPLETED, store_factory=db)
    pipe.discovery.done.set()
    pipe.enrichment.done.set()
    return pipe, seen


def test_single_session_lane_reuses_one_session_and_closes_it(db, monkeypatch):
    monkeypatch.setattr(L, "_wa_parallel", lambda: 1)
    pipe, seen = _lane_setup(db, monkeypatch, ["a1"], batches=3)
    lane = pipe.whatsapp
    lane.store = db()
    try:
        reason = lane.work()
    finally:
        lane.store.close()
    assert reason == L.R_COMPLETED, reason
    assert len(seen) == 3 and len({s[1] for s in seen}) == 1             # same session every batch
    assert len(RecSession.made) == 1 and RecSession.made[0].closed == 1  # closed once, at lane end
    assert lane._wa_session is None


def test_parallel_lane_keeps_one_worker_per_account(db, monkeypatch):
    monkeypatch.setattr(L, "_wa_parallel", lambda: 2)
    pipe, seen = _lane_setup(db, monkeypatch, ["a1", "a2"], batches=3)
    lane = pipe.whatsapp
    lane.store = db()
    try:
        reason = lane.work()
    finally:
        lane.store.close()
    assert reason == L.R_COMPLETED, reason
    for acc in ("a1", "a2"):
        mine = [s for s in seen if s[0] == acc]
        assert len(mine) == 3, seen
        assert len({s[1] for s in mine}) == 1 and len({s[2] for s in mine}) == 1   # same session + thread
    assert len(RecSession.made) == 2 and all(s.closed == 1 for s in RecSession.made)
    assert lane._wa_workers is None
