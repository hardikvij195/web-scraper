"""W110 (CRM T592): unlink (log out of WhatsApp Web, keep the account) and delete (unlink,
then wipe profile + row). The WhatsApp Web part is monkeypatched — it needs a real phone."""
from pathlib import Path

from webscraper import wa_verify
from webscraper.store import Store


def _setup(tmp_path: Path, monkeypatch):
    profiles = tmp_path / "wa-profiles"
    profiles.mkdir()
    monkeypatch.setattr(wa_verify.settings, "wa_profiles_dir", profiles)
    st = Store(tmp_path / "t.db")
    monkeypatch.setattr(wa_verify, "Store", lambda: st)
    monkeypatch.setattr(wa_verify, "login_in_progress", lambda name=None: False)
    (profiles / "main" / "Default").mkdir(parents=True)
    st.add_wa_account("main")
    return profiles, st


def test_unlink_and_delete_refuse_like_reset(tmp_path: Path, monkeypatch):
    profiles, st = _setup(tmp_path, monkeypatch)
    for fn in (wa_verify.unlink_account, wa_verify.delete_account):
        assert fn("Bad Name")[0] is False
        assert fn("main", busy=True)[0] is False
    monkeypatch.setattr(wa_verify, "login_in_progress", lambda name=None: True)
    assert wa_verify.unlink_account("main")[0] is False
    assert wa_verify.delete_account("main")[0] is False
    assert (profiles / "main").exists()          # refusals touch nothing
    st.close()


def test_unlink_without_a_saved_session_is_a_noop(tmp_path: Path, monkeypatch):
    profiles, st = _setup(tmp_path, monkeypatch)
    ok, msg = wa_verify.unlink_account("spare1")  # no profile folder
    assert ok and "no saved session" in msg
    st.close()


def test_unlink_keeps_the_account(tmp_path: Path, monkeypatch):
    profiles, st = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(wa_verify, "_log_out_of_whatsapp", lambda name: (True, "unlinked — removed from the phone's Linked devices"))
    ok, msg = wa_verify.unlink_account("main")
    assert ok and "unlinked" in msg
    assert (profiles / "main").exists()
    assert "main" in {a["name"] for a in st.list_wa_accounts()}
    st.close()


def test_delete_wipes_even_when_logout_fails(tmp_path: Path, monkeypatch):
    profiles, st = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(wa_verify, "_log_out_of_whatsapp", lambda name: (False, "could not find WhatsApp Web's Log out"))
    ok, msg = wa_verify.delete_account("main")
    assert ok and "NOT logged out" in msg          # user choice: delete anyway + warn
    assert not (profiles / "main").exists()
    assert "main" not in {a["name"] for a in st.list_wa_accounts()}
    st.close()


def test_delete_after_logout(tmp_path: Path, monkeypatch):
    profiles, st = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(wa_verify, "_log_out_of_whatsapp", lambda name: (True, "unlinked"))
    ok, msg = wa_verify.delete_account("main")
    assert ok and "logged out of WhatsApp" in msg
    assert not (profiles / "main").exists()
    st.close()
