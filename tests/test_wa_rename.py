"""W80: renaming a WhatsApp account moves its profile dir and its store row, and refuses
while anything could be holding the profile."""
from __future__ import annotations

from pathlib import Path

import pytest

from webscraper import wa_verify
from webscraper.store import Store


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    profiles = tmp_path / "wa-profiles"
    profiles.mkdir()
    monkeypatch.setattr(wa_verify.settings, "wa_profiles_dir", profiles)
    st = Store(tmp_path / "t.db")
    yield profiles, st
    st.close()


def test_rename_moves_dir_and_row(env):
    profiles, st = env
    (profiles / "spare1" / "Default").mkdir(parents=True)
    st.add_wa_account("spare1"); st.set_wa_status("spare1", "logged_in")
    ok, msg = wa_verify.rename_account("spare1", "sales", store=st)
    assert ok, msg
    assert (profiles / "sales" / "Default").exists() and not (profiles / "spare1").exists()
    rows = {r["name"]: r for r in st.list_wa_accounts()}
    assert "sales" in rows and "spare1" not in rows
    assert rows["sales"]["status"] == "logged_in"


def test_rename_refusals(env, monkeypatch):
    profiles, st = env
    (profiles / "main").mkdir(); st.add_wa_account("main")
    (profiles / "taken").mkdir(); st.add_wa_account("taken")
    assert wa_verify.rename_account("main", "taken", store=st)[0] is False
    assert wa_verify.rename_account("main", "Bad Name", store=st)[0] is False
    assert wa_verify.rename_account("main", "main", store=st)[0] is False
    assert wa_verify.rename_account("ghost", "x", store=st)[0] is False
    assert wa_verify.rename_account("main", "x", busy=True, store=st)[0] is False
    monkeypatch.setattr(wa_verify, "login_in_progress", lambda name=None: True)
    assert wa_verify.rename_account("main", "x", store=st)[0] is False
    assert (profiles / "main").exists()
