"""W89: per-machine pause between WhatsApp checks."""
from webscraper import wa_verify


def test_delay_range(monkeypatch):
    monkeypatch.setattr(wa_verify, "DEVICE_NAME", "1 - PC", raising=False)
    monkeypatch.delenv("WA_DELAY", raising=False)
    monkeypatch.delenv("WA_DELAY__1 - PC", raising=False)
    lo, hi = wa_verify.wa_delay_range()
    assert (lo, hi) == (wa_verify.settings.wa_delay_min, wa_verify.settings.wa_delay_max)
    monkeypatch.setenv("WA_DELAY", "1-3")
    assert wa_verify.wa_delay_range() == (1.0, 3.0)
    monkeypatch.setenv("WA_DELAY", "0.1-0.2")          # floor
    assert wa_verify.wa_delay_range() == (0.5, 0.5)
    monkeypatch.setenv("WA_DELAY", "6-2")              # inverted → clamped
    assert wa_verify.wa_delay_range() == (6.0, 6.0)
    monkeypatch.setenv("WA_DELAY", "nonsense")
    assert wa_verify.wa_delay_range() == (wa_verify.settings.wa_delay_min, wa_verify.settings.wa_delay_max)
