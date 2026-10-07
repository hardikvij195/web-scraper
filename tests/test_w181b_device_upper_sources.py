"""W181b: the per-device Chrome-diet flags resolve the device name from the agent's exported
env / the persisted `data/device_name` file — not only from a silent `webscraper.agent` import
(2 - MAC kept blocking Maps assets after `MAPS_BLOCK_ASSETS__2 - MAC=0` arrived from the cloud)."""
from __future__ import annotations

import builtins
import logging

from webscraper import chrome_args, config, maps


def _clear(monkeypatch):
    for k in ("HVT_AGENT_DEVICE", "MAPS_BLOCK_ASSETS", "MAPS_BLOCK_ASSETS__2 - MAC",
              "CHROME_LEAN_ARGS", "CHROME_LEAN_ARGS__2 - MAC"):
        monkeypatch.delenv(k, raising=False)


def test_env_device_wins_and_per_device_flag_is_honoured(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setattr(config, "ROOT", tmp_path)                  # no data/device_name file
    monkeypatch.setenv("HVT_AGENT_DEVICE", "2 - mac")
    assert chrome_args.device_upper() == "2 - MAC"
    monkeypatch.setenv("MAPS_BLOCK_ASSETS", "1")
    monkeypatch.setenv("MAPS_BLOCK_ASSETS__2 - MAC", "0")
    assert chrome_args.flag_enabled("MAPS_BLOCK_ASSETS") is False
    assert maps.block_assets_enabled() is False
    monkeypatch.delenv("MAPS_BLOCK_ASSETS__2 - MAC")
    assert chrome_args.flag_enabled("MAPS_BLOCK_ASSETS") is True   # generic wins again


def test_device_name_file_used_without_env(monkeypatch, tmp_path):
    _clear(monkeypatch)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "device_name").write_text("2 - MAC\n", encoding="utf-8")
    monkeypatch.setattr(config, "ROOT", tmp_path)
    assert chrome_args.device_name() == "2 - MAC"
    monkeypatch.setenv("CHROME_LEAN_ARGS__2 - MAC", "0")
    assert chrome_args.enabled() is False and chrome_args.lean_args("maps") == []


def test_decision_log_lines_carry_the_inputs(monkeypatch, tmp_path, caplog):
    _clear(monkeypatch)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setenv("HVT_AGENT_DEVICE", "2 - MAC")
    monkeypatch.setenv("MAPS_BLOCK_ASSETS__2 - MAC", "0")
    monkeypatch.setenv("CHROME_LEAN_ARGS__2 - MAC", "0")
    with caplog.at_level(logging.INFO):
        assert maps.block_assets_decision() is False
        assert chrome_args.lean_args("maps") == []
    assert "Maps assets NOT blocked (W181 switch off) [device='2 - MAC' per-device='0' generic=None]" in caplog.text
    assert "Chrome diet off for this device (W181) [device='2 - MAC' per-device='0' generic=None]" in caplog.text
    caplog.clear()
    monkeypatch.delenv("MAPS_BLOCK_ASSETS__2 - MAC")
    with caplog.at_level(logging.INFO):
        assert maps.block_assets_decision() is True
    assert "Maps assets blocked: images/media/fonts (W169) [device='2 - MAC' per-device=None generic=None]" in caplog.text


def test_import_failure_is_logged_once_not_swallowed(monkeypatch, tmp_path, caplog):
    _clear(monkeypatch)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(chrome_args, "_import_warned", False)
    real_import = builtins.__import__

    def boom(name, *a, **kw):
        if name == "webscraper.agent":
            raise RuntimeError("circular import under test")
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", boom)
    with caplog.at_level(logging.WARNING):
        assert chrome_args.device_name() == ""
        assert chrome_args.device_name() == ""
    assert caplog.text.count("device name unresolved") == 1
    assert "circular import under test" in caplog.text
