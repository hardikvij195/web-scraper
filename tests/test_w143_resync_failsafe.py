"""W143 (CRM T1021): the WhatsApp re-sync failsafe. MAC 2026-10-05, 09:29-15:05: every browser load
sat on "messages are downloading", the W137 bench fired, the lane parked 5 min, and the same again —
92 pauses, 0 verdicts, nobody told (each job's lane restarted the park count from zero).

  * per-ACCOUNT ladder of zero-verdict re-sync episodes: 1-2 pause · 3 full browser recovery (kill the
    profile holder, clear lock files, relaunch, ONE long sync wait) · 4 `needs_relink` (out of rotation,
    one clear error line) · a decided verdict resets it
  * a flagged account is out of `pick_wa_account` / `enabled_wa_accounts`; `verify_places` raises
    WaNotLoggedIn naming it; the self-check says `main: NEEDS RELINK (sync never finished, since HH:MM)`
  * the W131 recycle is held off for WA_RECYCLE_BACKOFF_CHECKS checks after a slow (> 60 s) sync
  * the WhatsApp lane: parks on episodes 1-2, no park on `needs_relink`, then takes the relink wait

Fakes only: no Playwright, no Chrome, no `data/leads.db`.
"""
from __future__ import annotations

import pytest

from webscraper import browser_recovery as br, lanes as L, wa_verify as wv
from tests.test_w115_wa_sync_requeue import _Page, _Store, _rows, wa  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _clean_ladder():
    for d in (wv._RESYNC_EPISODES, wv._RESYNC_FIRST_AT, wv._RECYCLE_HOLD, wv._LAST_SYNC_SEC,
              wv._SYNC_MAX_OVERRIDE, wv._CHECKS_SINCE_RECYCLE):
        d.clear()
    yield
    for d in (wv._RESYNC_EPISODES, wv._RESYNC_FIRST_AT, wv._RECYCLE_HOLD, wv._LAST_SYNC_SEC,
              wv._SYNC_MAX_OVERRIDE, wv._CHECKS_SINCE_RECYCLE):
        d.clear()


class _S(_Store):
    """Store fake with the W143 flag."""
    def __init__(self):
        self.flags: dict[str, bool] = {}
        self.logs: list[str] = []

    def list_wa_accounts(self):
        return [{"name": "acc1", "disabled": 0, "needs_relink": int(self.flags.get("acc1", False))}]

    def set_wa_needs_relink(self, name, flag):
        self.flags[name] = flag

    def log(self, job_id, lane, message, level="info"):
        self.logs.append(message)

    def record_wa_check(self, *a, **k):
        raise AssertionError("a syncing session must never record a verdict")


def _always_syncing(monkeypatch):
    monkeypatch.setattr(wv, "_decide", lambda page: "unknown")
    monkeypatch.setattr(wv, "_boot_state", lambda page: "syncing")
    monkeypatch.setattr(wv, "wait_boot", lambda page, name, **kw: "syncing")


def test_ladder_steps_are_pure():
    assert [wv.resync_step(n) for n in (1, 2, 3, 4, 9)] == ["pause", "pause", "recover", "relink", "relink"]
    assert wv.WA_RESYNC_RECOVERY_EPISODE == 3 and wv.WA_RESYNC_RELINK_EPISODE == 4


def test_episodes_pause_then_recover_then_relink(wa, monkeypatch):
    _always_syncing(monkeypatch)
    killed: list[str] = []
    monkeypatch.setattr(wv._br, "kill_profile_holder", lambda d, reason="": killed.append(f"{d.name}:{reason}") or True)
    monkeypatch.setenv("WA_RESYNC_LONG_WAIT_SEC", "123")
    seen_window: list[float | None] = []

    class _Ctx:
        def close(self):
            pass

    def ensure(pw, open_ctx, rl, name, headless=None):
        seen_window.append(wv._SYNC_MAX_OVERRIDE.get(name))
        return open_ctx.setdefault(name, (_Ctx(), _Page()))[1]
    monkeypatch.setattr(wv, "_ensure_session", ensure)
    st = _S()

    r1 = wv.verify_places(st, _rows(25), job_id=7)            # episode 1: pause (as W137)
    assert r1["sync_blocked"] is True and r1["needs_relink"] == 0
    r2 = wv.verify_places(st, _rows(25), job_id=7)            # episode 2: pause
    assert r2["sync_blocked"] is True and wv._RESYNC_EPISODES["acc1"] == 2
    assert not killed

    r3 = wv.verify_places(st, _rows(25), job_id=7)            # episode 3: recovery + ONE long wait
    assert killed == ["acc1:W143 re-sync recovery"]
    assert 123.0 in seen_window, "the relaunch after recovery must boot with the long sync window"
    assert wv._SYNC_MAX_OVERRIDE == {}, "the long window is one-off"
    assert r3["sync_blocked"] is False and r3["needs_relink"] == 0   # synced: the lane goes straight on
    assert any("full browser recovery" in m for m in st.logs)
    assert st.flags == {}

    r4 = wv.verify_places(st, _rows(25), job_id=7)            # episode 4: needs_relink, no more pauses
    assert r4["sync_blocked"] is False and r4["needs_relink"] == 1
    assert st.flags == {"acc1": True}
    assert any(m.startswith("WhatsApp [acc1] needs a fresh QR relink — WhatsApp Web never finished syncing (4 episodes, ")
               for m in st.logs)
    assert "acc1" not in wv._RESYNC_EPISODES

    with pytest.raises(wv.WaNotLoggedIn, match="need a fresh QR relink: acc1"):
        wv.verify_places(st, _rows(1), job_id=7)              # flagged = no account to pick


def test_failed_recovery_keeps_the_lane_parked(wa, monkeypatch):
    _always_syncing(monkeypatch)
    monkeypatch.setattr(wv._br, "kill_profile_holder", lambda d, reason="": True)
    wv._RESYNC_EPISODES["acc1"] = 2

    class _Ctx:
        def close(self):
            pass

    def ensure(pw, open_ctx, rl, name, headless=None):
        if wv._SYNC_MAX_OVERRIDE.get(name):                      # the recovery relaunch: still syncing
            e = wv.WaUnavailable("still downloading")
            e.syncing = True
            raise e
        return open_ctx.setdefault(name, (_Ctx(), _Page()))[1]
    monkeypatch.setattr(wv, "_ensure_session", ensure)
    res = wv.verify_places(_S(), _rows(25), job_id=7)
    assert res["sync_blocked"] is True and res["needs_relink"] == 0
    assert wv._RESYNC_EPISODES["acc1"] == 3


def test_sync_out_at_boot_is_an_episode(wa, monkeypatch):
    def ensure(pw, open_ctx, rl, name, headless=None):
        e = wv.WaUnavailable("[acc1] WhatsApp Web is still downloading messages after 360s")
        e.syncing = True
        raise e
    monkeypatch.setattr(wv, "_ensure_session", ensure)
    res = wv.verify_places(_S(), _rows(3), job_id=7)
    assert res["sync_blocked"] is True and wv._RESYNC_EPISODES["acc1"] == 1


def test_a_decided_verdict_resets_the_ladder(wa, monkeypatch):
    wv._RESYNC_EPISODES["acc1"] = 2
    wv._RESYNC_FIRST_AT["acc1"] = 1.0
    monkeypatch.setattr(wv, "_decide", lambda page: "yes")
    monkeypatch.setattr(wv, "_boot_state", lambda page: "chat")
    res = wv.verify_places(_Store(), _rows(1))
    assert res["yes"] == 1 and res["sync_blocked"] is False
    assert "acc1" not in wv._RESYNC_EPISODES and "acc1" not in wv._RESYNC_FIRST_AT


def test_lock_file_cleanup_list(tmp_path):
    assert set(br._LOCK_FILES) == {"SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile"}
    for n in br._LOCK_FILES:
        (tmp_path / n).write_text("x")
    (tmp_path / "Default").mkdir()                              # the session itself is never touched
    br._remove_lock_files(tmp_path)
    assert not any((tmp_path / n).exists() for n in br._LOCK_FILES)
    assert (tmp_path / "Default").is_dir()


def test_store_flag_and_checks_string(monkeypatch, tmp_path):
    from webscraper import healthcheck as hc
    from webscraper.store import Store
    path = tmp_path / "t.db"
    s = Store(path)
    s.add_wa_account("main")
    s.set_wa_status("main", "logged_in")
    assert s.enabled_wa_accounts() == ["main"] and s.pick_wa_account(0, "2026-10-05") == "main"
    s.set_wa_needs_relink("main", True)
    assert s.enabled_wa_accounts() == [] and s.pick_wa_account(0, "2026-10-05") is None
    row = s.list_wa_accounts()[0]
    assert row["needs_relink"] == 1 and row["needs_relink_at"]

    (tmp_path / "profiles" / "main" / "Default").mkdir(parents=True)
    monkeypatch.setattr(hc.settings, "wa_profiles_dir", tmp_path / "profiles")
    monkeypatch.setattr("webscraper.store.Store", lambda *a, **k: Store(path))
    chk = hc._wa_session()
    assert chk["ok"] is False
    assert chk["detail"].startswith("WhatsApp accounts — main: NEEDS RELINK (sync never finished, since ")
    assert chk["detail"].rstrip(")")[-5:].count(":") == 1          # HH:MM

    s.set_wa_needs_relink("main", False)                          # what a finished wa_login does
    assert s.enabled_wa_accounts() == ["main"]
    chk = hc._wa_session()
    assert chk["ok"] is True and "main: linked" in chk["detail"]


def test_recycle_backoff_after_a_slow_sync(wa, monkeypatch):
    monkeypatch.setenv("WA_RECYCLE_BACKOFF_CHECKS", "3")
    wv.note_sync_duration("acc1", 30)                             # fast sync: no hold
    assert wv.recycle_allowed("acc1") is True
    wv.note_sync_duration("acc1", 61)                             # slow sync: hold 3 checks
    assert wv._RECYCLE_HOLD["acc1"] == 3

    closed = {"n": 0}

    class _Ctx:
        def close(self):
            closed["n"] += 1
    monkeypatch.setattr(wv, "_ensure_session",
                        lambda pw, open_ctx, rl, name, headless=None: open_ctx.setdefault(name, (_Ctx(), _Page()))[1])
    monkeypatch.setattr(wv, "_decide", lambda page: "no")
    monkeypatch.setattr(wv, "_boot_state", lambda page: "chat")
    monkeypatch.setattr(wv, "wa_relaunch_every", lambda: 2)

    wv.verify_places(_Store(), _rows(3))                          # 3 checks: due at 2, but held
    assert closed["n"] == 1, "only the end-of-slice close — no W131 recycle while the hold counts down"
    assert wv._RECYCLE_HOLD["acc1"] == 0 and wv._CHECKS_SINCE_RECYCLE["acc1"] == 3
    wv.verify_places(_Store(), _rows(1))                          # hold spent: the recycle fires now
    assert wv._CHECKS_SINCE_RECYCLE["acc1"] == 0
    assert closed["n"] == 3                                       # recycle close + end-of-slice close


def test_lane_parks_twice_then_takes_the_relink_wait(monkeypatch, tmp_path):
    from webscraper.store import Store, now_iso
    path = tmp_path / "t.db"
    db = lambda: Store(path)                                           # noqa: E731
    s = db()
    job_id = s.create_job(query="q", location="here", max_places=10, delay_sec=0)
    s.conn.execute("INSERT INTO places(job_id, place_key, name, phone, enrich_status, scraped_at) VALUES (?,?,?,?,'done',?)",
                   (job_id, "p1", "biz", "+919999999999", now_iso()))
    s.conn.commit()
    s.add_wa_account("acc1")
    s.set_wa_status("acc1", "logged_in")
    s.close()
    job = {"do_enrich": 0, "do_research": 0, "do_wa_verify": 1, "country": "IN", "reenrich_only": 1}
    L.reset_stage_gates()
    monkeypatch.setattr(L, "WA_SYNC_PARK_SEC", 0.05)
    monkeypatch.setattr(L, "IDLE_POLL_SEC", 0.01)
    monkeypatch.setattr(L, "_wait_for_relink", lambda lane, store, err: False)
    monkeypatch.setattr(wv, "login_in_progress", lambda name=None: False)
    calls = {"n": 0}
    base = {"yes": 0, "no": 0, "unknown": 0, "checked": 0, "capped": 0, "no_number": 0}

    def fake_verify(store, batch, on_progress, should_stop, job_id=None, headless=None, account=None):
        calls["n"] += 1
        if calls["n"] <= 2:
            return {**base, "sync_blocked": True, "needs_relink": 0}       # episodes 1-2
        if calls["n"] == 3:
            return {**base, "sync_blocked": False, "needs_relink": 1}      # episode 4 flagged it
        raise wv.WaNotLoggedIn("WhatsApp account(s) need a fresh QR relink: acc1")
    monkeypatch.setattr(wv, "verify_places", fake_verify)

    pipe = L.Pipeline(job_id, job, lambda lane: L.R_COMPLETED, store_factory=db)
    pipe.discovery.start(); pipe.discovery.join(2)
    pipe.enrichment.start(); pipe.enrichment.join(2)
    gate = L.STAGE_GATES["whatsapp"]
    released = {"seen": False}
    lane = pipe.whatsapp
    orig = lane._slot_idle

    def spy(why, force=False):
        orig(why, force=force)
        released["seen"] = released["seen"] or not gate.holds(job_id)
    lane._slot_idle = spy
    lane.start()
    lane.join(5)
    assert not lane.is_alive()
    assert calls["n"] == 4
    assert released["seen"], "the WhatsApp slot must be released while parked"
    assert lane.reason == L.R_WA_LOGIN
    logs = [r["message"] for r in db().conn.execute("SELECT message FROM job_logs WHERE job_id=?", (job_id,))]
    assert sum("pausing WhatsApp checks" in m for m in logs) == 2
    assert any("flagged NEEDS RELINK" in m for m in logs)
    assert db().conn.execute("SELECT COUNT(*) FROM wa_checks WHERE job_id=?", (job_id,)).fetchone()[0] == 0
