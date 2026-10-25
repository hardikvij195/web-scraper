"""W198 (CRM T1391): WhatsApp account state is honest and self-healing.

1a the self-check label reflects `disabled=1` (NOT linked with QR proof, PAUSED without) ·
1b a disabled account with no QR proof is re-probed and re-enabled when it shows the chat list ·
1c an account re-enabled mid re-verify joins it · 2 a deleted account's row really goes, a logged-out
account never takes a start / join slot · 3 "database is locked" restarts a slice, never ends it.
No real WhatsApp: every probe / session is a fake."""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from webscraper import agent, healthcheck, lanes, wa_verify
from webscraper.config import settings
from webscraper.store import Store


def _rows(n: int) -> list[dict]:
    return [{"place_key": f"p{i}", "number": f"+9198765{i:05d}", "source": "maps"} for i in range(n)]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "leads.db")
    monkeypatch.setattr(settings, "wa_profiles_dir", tmp_path / "wa-profiles")
    s = Store(tmp_path / "leads.db")
    yield s
    s.close()


def _fake_verify(seen: dict, lock: threading.Lock, sec: float = 0.01):
    def fake(st, rows, on_progress=None, should_stop=None, job_id=None, headless=None, account=None, **_kw):
        n = 0
        for r in rows:
            time.sleep(sec)
            with lock:
                seen.setdefault(account, []).append(r["place_key"])
            on_progress(r["place_key"], "yes", r["number"].lstrip("+"), "maps")
            n += 1
        return {"yes": n, "no": 0, "unknown": 0, "checked": n, "capped": 0, "no_number": 0}
    return fake


def _acct(store: Store, name: str, status: str | None = "logged_in", disabled: bool = False) -> None:
    store.add_wa_account(name)
    if status:
        store.set_wa_status(name, status)
    if disabled:
        store.disable_wa_account(name, "logged_out", "session dropped mid-run")
    (settings.wa_profiles_dir / name / "Default").mkdir(parents=True, exist_ok=True)


# -- 1a ------------------------------------------------------------------------------------------
def test_label_reflects_disabled(store, monkeypatch):
    monkeypatch.setattr(lanes, "wa_off", lambda: False)
    _acct(store, "ok_one")
    _acct(store, "proven_out", "logged_in", disabled=True)
    store.set_wa_status("proven_out", "logged_out")                # QR screen seen = proof
    _acct(store, "false_off", "logged_in", disabled=True)          # disabled, no proof
    detail = healthcheck._wa_session()["detail"]
    parts = {p.split(":", 1)[0].replace("WhatsApp accounts — ", "").strip(): p.split(":", 1)[1]
             for p in detail.split(";")}
    assert parts["ok_one"].strip().startswith("linked")
    assert "NOT linked" in parts["proven_out"]
    # CRM `lead_gen_wa_usable`: strip NOT linked|NEEDS RELINK, then ~* 'linked' must be false
    for k in ("proven_out", "false_off"):
        rest = parts[k].replace("NOT linked", "").replace("NEEDS RELINK", "")
        assert "linked" not in rest.lower(), parts[k]
    assert "PAUSED" in parts["false_off"] and "NEEDS RELINK" not in parts["false_off"]
    assert "NOT linked" not in parts["false_off"]                  # no owner to-do without proof


# -- 1b ------------------------------------------------------------------------------------------
def test_logged_in_sighting_reenables_a_false_disable(store):
    _acct(store, "acc", disabled=True)
    assert "acc" not in store.enabled_wa_accounts()
    assert store.unproven_disabled_wa_accounts() == ["acc"]
    store.set_wa_status("acc", "logged_in")                        # e.g. wa_login "already linked"
    assert "acc" in store.enabled_wa_accounts()
    assert store.unproven_disabled_wa_accounts() == []


def test_reprobe_disabled_heals_or_keeps_by_proof(store, monkeypatch):
    _acct(store, "fine", disabled=True)
    _acct(store, "gone", disabled=True)
    _acct(store, "blank", disabled=True)
    _acct(store, "proven", "logged_out", disabled=True)
    probed: list[str] = []
    answers = {"fine": "logged_in", "gone": "logged_out", "blank": "unknown"}

    def probe(name):
        probed.append(name)
        st = answers[name]
        if st != "unknown":
            Store(store.path).set_wa_status(name, st)              # what account_status writes
        return st
    monkeypatch.setattr(lanes, "wa_off", lambda: False)
    monkeypatch.setattr(lanes, "_account_status", probe)
    monkeypatch.setattr(wa_verify, "login_in_progress", lambda n: False)
    notes: list[str] = []
    back = lanes.reprobe_disabled(store, note=lambda m, lvl: notes.append(m))
    assert back == ["fine"]
    assert "proven" not in probed                                  # QR proof already on file
    enabled = store.enabled_wa_accounts()
    assert "fine" in enabled and "gone" not in enabled and "blank" not in enabled
    assert store.unproven_disabled_wa_accounts() == ["blank"]       # retried next round
    assert len(notes) == 3


# -- 1c ------------------------------------------------------------------------------------------
def test_account_reenabled_mid_job_joins(store, monkeypatch):
    _acct(store, "a1")
    _acct(store, "a2", disabled=True)
    monkeypatch.setattr(agent, "WA_JOIN_POLL_SEC", 0.02)
    monkeypatch.setattr("webscraper.lanes._wa_parallel", lambda: 3)
    monkeypatch.setattr(agent, "_join_memory_ok", lambda: True)
    seen: dict = {}
    monkeypatch.setattr(wa_verify, "verify_places", _fake_verify(seen, threading.Lock(), 0.02))
    threading.Timer(0.1, lambda: Store(store.path).set_wa_status("a2", "logged_in")).start()
    rows = _rows(60)
    agent._verify_slices(store, rows, ["a1"], lambda *a, **k: None)
    assert seen.get("a2"), "the re-enabled account never joined"
    assert sorted(k for v in seen.values() for k in v) == sorted(r["place_key"] for r in rows)


def test_reverify_reprobes_a_false_disable_and_it_joins(store, monkeypatch):
    _acct(store, "a1")
    _acct(store, "a2", disabled=True)
    monkeypatch.setattr(agent, "WA_JOIN_POLL_SEC", 0.02)
    monkeypatch.setattr(agent, "WA_DISABLED_REPROBE_SEC", 0.05)
    monkeypatch.setattr("webscraper.lanes._wa_parallel", lambda: 3)
    monkeypatch.setattr(agent, "_join_memory_ok", lambda: True)
    monkeypatch.setattr(lanes, "wa_off", lambda: False)
    monkeypatch.setattr(wa_verify, "login_in_progress", lambda n: False)

    def probe(name):
        Store(store.path).set_wa_status(name, "logged_in")
        return "logged_in"
    monkeypatch.setattr(lanes, "_account_status", probe)
    seen: dict = {}
    monkeypatch.setattr(wa_verify, "verify_places", _fake_verify(seen, threading.Lock(), 0.02))
    agent._verify_slices(store, _rows(60), ["a1"], lambda *a, **k: None)
    assert seen.get("a2"), "the re-probed account never joined"


# -- 2 -------------------------------------------------------------------------------------------
def test_logged_out_account_never_joins(store, monkeypatch):
    _acct(store, "a1")
    _acct(store, "main", "logged_out")                             # enabled, but last seen on the QR
    monkeypatch.setattr(agent, "WA_JOIN_POLL_SEC", 0.02)
    monkeypatch.setattr("webscraper.lanes._wa_parallel", lambda: 3)
    monkeypatch.setattr(agent, "_join_memory_ok", lambda: True)
    seen: dict = {}
    monkeypatch.setattr(wa_verify, "verify_places", _fake_verify(seen, threading.Lock(), 0.005))
    agent._verify_slices(store, _rows(30), ["a1"], lambda *a, **k: None)
    assert "main" not in seen
    assert agent._wa_logged_out(store) == {"main"}


def test_delete_retries_a_locked_row_removal(store, monkeypatch):
    _acct(store, "main")
    monkeypatch.setattr(wa_verify, "login_in_progress", lambda n: False)
    monkeypatch.setattr(wa_verify.time, "sleep", lambda s: None)
    real = Store.remove_wa_account
    calls = {"n": 0}

    def flaky(self, name):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(self, name)
    monkeypatch.setattr(Store, "remove_wa_account", flaky)
    ok, _msg = wa_verify.reset_account("main")
    assert ok and calls["n"] == 2
    assert "main" not in [a["name"] for a in store.list_wa_accounts()]


def test_delete_says_so_when_the_row_stays(store, monkeypatch):
    _acct(store, "main")
    monkeypatch.setattr(wa_verify, "login_in_progress", lambda n: False)
    monkeypatch.setattr(wa_verify.time, "sleep", lambda s: None)

    def locked(self, name):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(Store, "remove_wa_account", locked)
    ok, msg = wa_verify.reset_account("main")
    assert not ok and "could not be removed" in msg


def test_wa_busy_counts_a_reverify():
    assert agent._wa_busy(None, 0) is False
    assert agent._wa_busy(None, 21525) is True
    assert agent._wa_busy(12, None) is True


# -- 3 -------------------------------------------------------------------------------------------
def test_database_locked_restarts_the_slice(store, monkeypatch):
    _acct(store, "a1")
    monkeypatch.setattr(agent, "WA_JOIN_POLL_SEC", 0.02)
    monkeypatch.setattr("webscraper.lanes._wa_parallel", lambda: 1)
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    seen: dict = {}
    inner = _fake_verify(seen, threading.Lock(), 0.0)
    calls = {"n": 0}

    def flaky(st, rows, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            rows.take()                                            # took one, lost it to the lock
            raise sqlite3.OperationalError("database is locked")
        return inner(st, rows, **kw)
    monkeypatch.setattr(wa_verify, "verify_places", flaky)
    rows = _rows(10)
    res = agent._verify_slices(store, rows, ["a1"], lambda *a, **k: None)
    assert calls["n"] >= 2 and len(seen["a1"]) >= 9
    assert "a1" in store.enabled_wa_accounts()                     # a lock never disables
    assert res["checked"] >= 9
