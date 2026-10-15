"""W190 (CRM T1240): a deleted WhatsApp account stays deleted, and an account last seen logged
out gets no session.

1 - PC, 2026-10-15/16: `main` was deleted at 21:09 but its profile folder survived the rmtree (a
Chrome still held it). The per-job status refresh walks the profile folders, probed `main`, and
`Store.set_wa_status` INSERTed a fresh, enabled row for it. The next re-verify ran "2 parallel
sessions (main, oneplus_biz)": main opened to the QR screen, 0 checked, 42 numbers handed over,
while the linked poco_biz sat idle — and the CRM card said "1 of 2 linked".
"""
from pathlib import Path

import pytest

from webscraper.store import Store


@pytest.fixture
def st(tmp_path: Path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def test_status_sighting_does_not_create_an_account(st):
    st.set_wa_status("ghost", "logged_out")
    st.set_wa_status("ghost2", "logged_in")
    assert st.list_wa_accounts() == []


def test_deleted_account_stays_deleted_after_a_probe(st):
    st.add_wa_account("main")
    st.remove_wa_account("main")
    st.set_wa_status("main", "logged_in")
    assert [r["name"] for r in st.list_wa_accounts()] == []


def test_login_still_records_status_for_its_new_account(st):
    st.add_wa_account("spare1")                                   # login() adds the row first
    st.set_wa_status("spare1", "logged_in")
    row = {r["name"]: r for r in st.list_wa_accounts()}["spare1"]
    assert row["status"] == "logged_in" and row["status_at"]


def test_logged_out_account_sorts_last_so_a_session_slot_goes_to_a_linked_one(st):
    for n in ("main", "oneplus_biz", "poco_biz"):
        st.add_wa_account(n)
    st.set_wa_status("main", "logged_out")
    st.set_wa_status("oneplus_biz", "logged_in")
    # still listed (T601: an enabled, logged-out account is what the lane waits on for a relink)
    # but last — `_reverify_wa` takes accounts[:n_par], so WA ×2 = the two usable ones
    assert st.enabled_wa_accounts() == ["oneplus_biz", "poco_biz", "main"]
    assert st.pick_wa_account(0, "2026-10-16") in ("oneplus_biz", "poco_biz")
    # a relink (login writes logged_in) puts it back in plain order
    st.set_wa_status("main", "logged_in")
    assert st.enabled_wa_accounts() == ["main", "oneplus_biz", "poco_biz"]


def test_orphan_profile_folder_is_removed(tmp_path: Path):
    from webscraper import server
    p = tmp_path / "main"
    (p / "Default").mkdir(parents=True)
    (p / "Default" / "x").write_text("x")
    server._drop_orphan_wa_profile(p)
    assert not p.exists()
