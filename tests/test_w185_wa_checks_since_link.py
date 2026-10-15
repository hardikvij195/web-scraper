"""W185 (CRM T1247-b): every WhatsApp account counts its number checks since the last QR link,
and each drop (logged out / needs_relink / sync_stuck / unlinked / deleted) is logged once with
that count — "dropped after N checks since link"."""
from __future__ import annotations

from pathlib import Path

import pytest

from webscraper import healthcheck, lanes
from webscraper import store as store_mod
from webscraper.store import Store


@pytest.fixture()
def st(tmp_path: Path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def _row(st: Store, name: str) -> dict:
    return {r["name"]: r for r in st.list_wa_accounts()}[name]


def _events(st: Store, name: str | None = None) -> list[dict]:
    q = "SELECT name, kind, checks_since_link FROM wa_events"
    args: tuple = ()
    if name:
        q += " WHERE name=?"
        args = (name,)
    return [dict(r) for r in st.conn.execute(q + " ORDER BY id", args)]


def test_counter_increments_and_resets_on_link(st):
    st.add_wa_account("a")
    for _ in range(3):
        st.bump_wa_account("a", "2026-10-15")
    assert _row(st, "a")["checks_since_link"] == 3
    assert _row(st, "a")["sent_today"] == 3
    st.mark_wa_linked("a")
    r = _row(st, "a")
    assert r["checks_since_link"] == 0 and r["linked_at"]
    assert _events(st, "a") == [{"name": "a", "kind": "linked", "checks_since_link": 3}]
    st.bump_wa_account("a", "2026-10-15")
    assert _row(st, "a")["checks_since_link"] == 1


@pytest.mark.parametrize("setter,kind", [
    (lambda s: s.set_wa_needs_relink("a", True), "needs_relink"),
    (lambda s: s.set_wa_sync_stuck("a", True), "sync_stuck"),
    (lambda s: s.disable_wa_account("a", "logged_out"), "logged_out"),
    (lambda s: s.disable_wa_account("a", "unlinked"), "unlinked"),
])
def test_drop_logged_once_with_count(st, setter, kind):
    st.add_wa_account("a")
    st.mark_wa_linked("a")
    for _ in range(7):
        st.bump_wa_account("a", "2026-10-15")
    setter(st)
    setter(st)                                    # a repeated set must not add a second row
    drops = [e for e in _events(st, "a") if e["kind"] != "linked"]
    assert drops == [{"name": "a", "kind": kind, "checks_since_link": 7}]
    assert st.wa_drop_events(10)[0]["kind"] == kind


def test_flag_cleared_then_set_again_logs_again(st):
    st.add_wa_account("a")
    st.set_wa_needs_relink("a", True)
    st.set_wa_needs_relink("a", False)
    st.set_wa_needs_relink("a", True)
    assert [e["kind"] for e in _events(st, "a")] == ["needs_relink", "needs_relink"]


def test_rename_moves_events_and_delete_keeps_history(st):
    st.add_wa_account("old")
    st.bump_wa_account("old", "2026-10-15")
    st.set_wa_sync_stuck("old", True)
    assert st.rename_wa_account("old", "new")
    assert _events(st, "old") == []
    assert [e["kind"] for e in _events(st, "new")] == ["sync_stuck"]
    st.remove_wa_account("new")
    assert [e["kind"] for e in _events(st, "new")] == ["sync_stuck", "deleted"]
    st.remove_wa_account("new")                   # no row any more: no second 'deleted'
    assert len(_events(st, "new")) == 2


def test_healthcheck_detail_still_parses(st, tmp_path, monkeypatch):
    profiles = tmp_path / "wa-profiles"
    for n in ("oneplus_biz", "spare"):
        (profiles / n / "Default").mkdir(parents=True)
        st.add_wa_account(n)
    st.set_wa_status("oneplus_biz", "logged_in")
    for _ in range(120):
        st.bump_wa_account("oneplus_biz", "2026-10-15")
    st.set_wa_needs_relink("spare", True)
    monkeypatch.setattr(healthcheck.settings, "wa_profiles_dir", profiles)
    monkeypatch.setattr(store_mod, "Store", lambda *a, **k: st)
    monkeypatch.setattr(lanes, "wa_off", lambda: False)
    c = healthcheck._wa_session()
    assert c["detail"].startswith("WhatsApp accounts — ")
    parts = c["detail"][len("WhatsApp accounts — "):].split(";")
    assert len(parts) == 2
    name0, rest0 = parts[0].strip().split(":", 1)
    assert name0 == "oneplus_biz"
    assert rest0.strip().startswith("linked") and rest0.endswith("· 120 checks since link")
    name1, rest1 = parts[1].strip().split(":", 1)
    assert name1 == "spare" and rest1.strip().startswith("NEEDS RELINK")
    assert rest1.endswith("· 0 checks since link")
    assert c["drops"] == [{"name": "spare", "at": c["drops"][0]["at"], "kind": "needs_relink", "checks": 0}]
