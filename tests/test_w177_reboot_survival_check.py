"""W177 (2026-10-07): the `reboot_survival` self-check reports — without changing anything —
whether a Windows agent laptop will come back after an unattended reboot (auto-logon + the
AtLogOn task, W173) and stay awake under the agent (sleep / hibernate / lid, W175). The owner
ran the two one-time scripts un-elevated on DELL/ASUS and nobody could tell whether they
took; the CRM now shows it via `lead_gen_agents.checks`.
"""
from __future__ import annotations

import pytest

from webscraper import healthcheck as hc


def _powercfg_fake(table: dict[tuple[str, str], tuple[int | None, int | None]]):
    def fake(sub: str, setting: str) -> tuple[int | None, int | None]:
        return table[(sub, setting)]
    return fake


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(hc.platform, "system", lambda: "Windows")


def test_all_good_is_ok(windows, monkeypatch):
    monkeypatch.setattr(hc, "_winlogon", lambda: {"auto": "1", "user": "hardik"})
    monkeypatch.setattr(hc, "_win_task_present", lambda: True)
    monkeypatch.setattr(hc, "_powercfg", _powercfg_fake({
        ("SUB_SLEEP", "STANDBYIDLE"): (0, 0),
        ("SUB_SLEEP", "HIBERNATEIDLE"): (0, 0),
        ("SUB_BUTTONS", "LIDACTION"): (0, 0),
    }))
    c = hc._reboot_survival()
    assert c["ok"] is True and c.get("optional") is True
    assert c["detail"] == "auto-logon ON · task OK · sleep AC never/DC never · lid do nothing"
    assert c["fix"] == ""


def test_autologon_off_and_dc_sleep_not_ok(windows, monkeypatch):
    monkeypatch.setattr(hc, "_winlogon", lambda: {"auto": "0", "user": "hardik"})
    monkeypatch.setattr(hc, "_win_task_present", lambda: True)
    monkeypatch.setattr(hc, "_powercfg", _powercfg_fake({
        ("SUB_SLEEP", "STANDBYIDLE"): (0, 600),
        ("SUB_SLEEP", "HIBERNATEIDLE"): (0, 0),
        ("SUB_BUTTONS", "LIDACTION"): (1, 1),
    }))
    c = hc._reboot_survival()
    assert c["ok"] is False
    d = c["detail"]
    assert "auto-logon OFF" in d and "sleep AC never/DC 600s" in d and "lid sleep" in d
    assert "scripts\\enable-autologon.ps1" in d and "scripts\\set-agent-power.ps1" in d
    assert "as Administrator" in d
    assert "set-agent-power.ps1" in c["fix"]


def test_desktop_without_lid_is_ok(windows, monkeypatch):
    monkeypatch.setattr(hc, "_winlogon", lambda: {"auto": "1", "user": "hardik"})
    monkeypatch.setattr(hc, "_win_task_present", lambda: True)
    monkeypatch.setattr(hc, "_powercfg", _powercfg_fake({
        ("SUB_SLEEP", "STANDBYIDLE"): (0, None),
        ("SUB_SLEEP", "HIBERNATEIDLE"): (0, None),
        ("SUB_BUTTONS", "LIDACTION"): (None, None),
    }))
    c = hc._reboot_survival()
    assert c["ok"] is True and "lid n/a" in c["detail"]


def test_non_windows_is_na(monkeypatch):
    monkeypatch.setattr(hc.platform, "system", lambda: "Darwin")
    c = hc._reboot_survival()
    assert c["ok"] is True and c["detail"] == "n/a (not Windows)" and c.get("optional") is True


def test_in_standard_check_list(monkeypatch):
    monkeypatch.setattr(hc, "_reboot_survival", lambda: hc._check(True, "stub", optional=True))
    assert hc.run_checks()["checks"]["reboot_survival"]["detail"] == "stub"


def test_helpers_never_raise(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no powercfg here")
    monkeypatch.setattr(hc.subprocess, "run", boom)
    assert hc._powercfg("SUB_SLEEP", "STANDBYIDLE") == (None, None)
    assert hc._win_task_present() is False
    assert set(hc._winlogon()) == {"auto", "user"}
