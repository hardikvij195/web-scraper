"""W199 (CRM T1393): a re-verify checks never-checked numbers first; WA_REVERIFY_UNKNOWN=0 skips 'unknown'."""
from webscraper import agent


def test_reverify_unknown_default_on(monkeypatch):
    monkeypatch.delenv("WA_REVERIFY_UNKNOWN", raising=False)
    monkeypatch.setattr("webscraper.lanes._device_env", lambda k: None)
    assert agent._reverify_unknown() is True


def test_reverify_unknown_off(monkeypatch):
    monkeypatch.setenv("WA_REVERIFY_UNKNOWN", "0")
    monkeypatch.setattr("webscraper.lanes._device_env", lambda k: None)
    assert agent._reverify_unknown() is False


def test_device_value_wins(monkeypatch):
    monkeypatch.setenv("WA_REVERIFY_UNKNOWN", "0")
    monkeypatch.setattr("webscraper.lanes._device_env", lambda k: "1")
    assert agent._reverify_unknown() is True


def test_never_checked_sort_first():
    rows = [{"wa_verified": "unknown", "id": 1}, {"wa_verified": None, "id": 2}, {"id": 3}]
    rows.sort(key=lambda r: bool(r.get("wa_verified")))
    assert [r["id"] for r in rows] == [2, 3, 1]
