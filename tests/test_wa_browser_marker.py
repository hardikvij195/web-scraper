"""W93 (CRM T531): the browser that linked a WhatsApp profile is the one every later open uses,
and a client that never renders skips the account for the run instead of unlinking it."""
from webscraper import wa_verify
from webscraper.store import Store


def test_marker_wins_over_machine_default(tmp_path, monkeypatch):
    monkeypatch.setattr(wa_verify.settings, "wa_profiles_dir", tmp_path)
    monkeypatch.setattr(wa_verify, "_chrome_installed", lambda: True)
    wa_verify._BROWSER_FELL_BACK.clear()
    assert wa_verify.browser_for("main") == "chrome"             # nothing remembered yet
    wa_verify.remember_browser("main", "chromium")
    assert wa_verify.browser_for("main") == "chromium"           # the profile's own marker
    assert wa_verify.browser_for("spare1") == "chrome"           # others untouched
    assert wa_verify._launch_kwargs("visible", "main").get("channel") is None
    assert wa_verify._launch_kwargs("visible", "spare1").get("channel") == "chrome"
    # headless with bundled Chromium degrades to hidden (W65) — never headless Chromium
    kw = wa_verify._launch_kwargs("headless", "main")
    assert "--headless=new" not in kw["args"] and any(a.startswith("--window-position") for a in kw["args"])


def test_machine_preference_after_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(wa_verify.settings, "wa_profiles_dir", tmp_path)
    monkeypatch.setattr(wa_verify, "_chrome_installed", lambda: True)
    wa_verify._BROWSER_FELL_BACK.clear()
    wa_verify.remember_browser("main", "chromium", machine=True)
    assert wa_verify.browser_for("brand_new") == "chromium"      # new logins start on what works
    assert wa_verify.browser_for() == "chromium"


def test_no_chrome_means_chromium(tmp_path, monkeypatch):
    monkeypatch.setattr(wa_verify.settings, "wa_profiles_dir", tmp_path)
    monkeypatch.setattr(wa_verify, "_chrome_installed", lambda: False)
    wa_verify._BROWSER_FELL_BACK.clear()
    wa_verify.remember_browser("main", "chrome")                 # a stale marker from another box
    assert wa_verify.browser_for("main") == "chromium"
    assert wa_verify.other_browser("chromium") is None
    assert wa_verify.other_browser("chrome") == "chromium"


def test_in_run_fallback_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(wa_verify.settings, "wa_profiles_dir", tmp_path)
    monkeypatch.setattr(wa_verify, "_chrome_installed", lambda: True)
    wa_verify._BROWSER_FELL_BACK.clear()
    wa_verify._BROWSER_FELL_BACK["main"] = "chromium"
    assert wa_verify.browser_for("main") == "chromium"
    wa_verify._BROWSER_FELL_BACK.clear()


def test_pick_wa_account_exclude(tmp_path):
    st = Store(tmp_path / "t.db")
    st.add_wa_account("main")
    st.add_wa_account("spare1")
    assert st.pick_wa_account(0, "2026-09-09") in ("main", "spare1")
    assert st.pick_wa_account(0, "2026-09-09", exclude={"main"}) == "spare1"
    assert st.pick_wa_account(0, "2026-09-09", exclude={"main", "spare1"}) is None
    # the flag is untouched — an excluded account is still enabled
    assert st.conn.execute("SELECT disabled FROM wa_accounts WHERE name='main'").fetchone()[0] == 0


def test_unavailable_is_not_logged_out():
    assert not issubclass(wa_verify.WaUnavailable, wa_verify.WaNotLoggedIn)
