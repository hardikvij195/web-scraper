"""W194 (CRM T1344-b): per-machine memory start limit `MEMORY_START_MAX_PCT__<DEVICE>`."""
from webscraper import agent, server as server_mod


def test_start_limit_raw_prefers_device():
    assert server_mod.start_limit_raw("85", "75") == "85"
    assert server_mod.start_limit_raw("", "75") == "75"
    assert server_mod.start_limit_raw(None, None) is None


def test_memory_start_max_pct_per_device_wins(monkeypatch):
    monkeypatch.setattr(agent, "DEVICE_NAME", "5 - MI")
    monkeypatch.setattr(server_mod, "last_memory", lambda: {"total_mb": 8000.0})
    monkeypatch.delenv("MEMORY_START_MAX_PCT", raising=False)
    monkeypatch.delenv("MEMORY_START_MAX_PCT__5 - MI", raising=False)
    assert server_mod.memory_start_max_pct() == 75.0          # low-RAM default
    monkeypatch.setenv("MEMORY_START_MAX_PCT", "70")
    assert server_mod.memory_start_max_pct() == 70.0          # global
    monkeypatch.setenv("MEMORY_START_MAX_PCT__5 - MI", "85")
    assert server_mod.memory_start_max_pct() == 85.0          # per-device wins
    monkeypatch.setattr(agent, "DEVICE_NAME", "3 - ASUS")
    assert server_mod.memory_start_max_pct() == 70.0          # other machines untouched


def test_refresh_prefix_covers_device_key():
    assert "MEMORY_START_MAX_PCT__5 - MI".startswith(agent._CONFIG_REFRESH_PREFIXES)
    assert not "MEMORY_START_MAX_PCT".startswith(agent._CONFIG_REFRESH_PREFIXES)
