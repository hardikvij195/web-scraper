"""W86: per-machine WhatsApp window mode."""
from webscraper import wa_verify


def test_mode_resolution(monkeypatch):
    monkeypatch.setattr(wa_verify, "DEVICE_NAME", "1 - PC", raising=False)
    monkeypatch.delenv("WA_WINDOW", raising=False)
    monkeypatch.delenv("WA_WINDOW__1 - PC", raising=False)
    assert wa_verify.wa_window_mode() == "visible"
    monkeypatch.setenv("WA_WINDOW", "Hidden")
    assert wa_verify.wa_window_mode() == "hidden"
    monkeypatch.setenv("WA_WINDOW", "nonsense")
    assert wa_verify.wa_window_mode() == "visible"


def test_launch_kwargs():
    v = wa_verify._launch_kwargs("visible")
    h = wa_verify._launch_kwargs("hidden")
    n = wa_verify._launch_kwargs("headless")
    assert v["headless"] is False and "channel" not in v
    assert h["channel"] == "chrome" and any(a.startswith("--window-position=-32000") for a in h["args"])
    assert n["channel"] == "chrome" and "--headless=new" in n["args"]
    for k in (v, h, n):
        assert k["headless"] is False          # real Chrome new-headless is an ARG, never headless=True
