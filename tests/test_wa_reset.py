"""W91: reset an account (profile dir + row); refuse while busy / login open."""
from pathlib import Path

from webscraper import wa_verify
from webscraper.store import Store


def test_reset_account(tmp_path: Path, monkeypatch):
    profiles = tmp_path / "wa-profiles"
    profiles.mkdir()
    monkeypatch.setattr(wa_verify.settings, "wa_profiles_dir", profiles)
    st = Store(tmp_path / "t.db")
    monkeypatch.setattr(wa_verify, "Store", lambda: st)
    (profiles / "main" / "Default").mkdir(parents=True)
    st.add_wa_account("main")
    # W142: a job in flight PAUSES the lane for this account and runs, instead of refusing
    paused = []
    monkeypatch.setattr(wa_verify, "with_lane_paused",
                        lambda name, fn, **kw: (paused.append(name), (True, "ran under pause"))[1])
    assert wa_verify.reset_account("main", busy=True) == (True, "ran under pause") and paused == ["main"]
    monkeypatch.setattr(wa_verify, "login_in_progress", lambda name=None: True)
    assert wa_verify.reset_account("main")[0] is False
    monkeypatch.setattr(wa_verify, "login_in_progress", lambda name=None: False)
    ok, msg = wa_verify.reset_account("main")
    assert ok, msg
    assert not (profiles / "main").exists()
    assert "main" not in {a["name"] for a in st.list_wa_accounts()}
    assert wa_verify.reset_account("Bad Name")[0] is False
    st.close()
