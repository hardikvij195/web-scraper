"""W182 (CRM T1078, DELL 2026-10-08 19:00 -> next morning): the agent's watchdog did `os._exit(3)`,
the supervisor `run-agent-loop.bat` then hung in `git pull` on a half-dead network (no timeout, no
`GIT_TERMINAL_PROMPT=0`) and `agent-autostart.vbs` saw that cmd.exe and never relaunched — 14.8 h offline
while the laptop was on. Three guards now:
  * the agent touches `data/agent.alive` on every successful CRM poll (`_loop_wd_mark("ok")`), so the
    autostart script can tell a hung loop from a healthy one;
  * both supervisor loops run git/pip with stall timeouts and never prompt;
  * `scripts/fix-agent.ps1` / `.sh` are the owner's one-shot repair (PowerShell 5.1: no `&&` / `||`).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import webscraper.config as config
from webscraper import agent

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    return tmp_path


def test_ok_mark_touches_agent_alive(root):
    agent._loop_wd_mark("ok")
    p = root / "data" / "agent.alive"
    assert p.is_file(), "a successful poll must leave data/agent.alive behind"
    first = p.stat().st_mtime
    agent._touch_alive()
    assert p.stat().st_mtime >= first


def test_attempt_and_err_marks_do_not_touch(root):
    agent._loop_wd_mark("attempt")
    agent._loop_wd_mark("err")
    assert not (root / "data" / "agent.alive").exists()
    assert "attempt" in agent._LOOP_WD and "err" in agent._LOOP_WD


def test_touch_never_raises(root, monkeypatch):
    # data/ is a FILE here, so mkdir fails: the poll loop must shrug it off.
    (root / "data").write_text("not a dir", encoding="utf-8")
    agent._loop_wd_mark("ok")             # no exception


@pytest.mark.parametrize("name", ["run-agent-loop.bat", "run-agent-loop.sh"])
def test_supervisor_loops_have_timeouts(name):
    text = (REPO / name).read_text(encoding="utf-8")
    assert "GIT_TERMINAL_PROMPT=0" in text
    assert "http.lowSpeedLimit=1000" in text and "http.lowSpeedTime=60" in text
    assert "--timeout 30 --retries 2" in text


def test_fix_agent_ps1_is_powershell_51_safe():
    lines = (REPO / "scripts" / "fix-agent.ps1").read_text(encoding="utf-8").splitlines()
    code = [l for l in lines if not l.strip().startswith("#")]
    bad = [l for l in code if "&&" in l or "||" in l or "??" in l]
    assert not bad, bad
    assert any(l.strip().startswith("param([switch]$Check)") for l in code)
    joined = "\n".join(code)
    for needle in ("agent up (", "AGENT UP", "NOT UP", "turns online within ~1 minute",
                   "GIT_TERMINAL_PROMPT", "http.lowSpeedTime=60", "--timeout 30 --retries 2",
                   "HVT Lead Finder Agent", "install-agent-autostart.ps1", "webscraper doctor"):
        assert needle in joined, needle


def test_fix_agent_sh_mirrors_the_windows_script():
    text = (REPO / "scripts" / "fix-agent.sh").read_text(encoding="utf-8")
    for needle in ("--check", "app.hvtechnologies.leadfinder-agent", "launchctl kickstart -k",
                   "launchctl bootstrap", "run-agent-loop.sh", "agent up (", "AGENT UP", "NOT UP",
                   "GIT_TERMINAL_PROMPT=0", "--timeout 30 --retries 2", "turns online within ~1 minute"):
        assert needle in text, needle
    assert "mapfile" not in text and "declare -A" not in text     # bash 3.2


def test_autostart_vbs_self_heals_a_stale_loop():
    text = (REPO / "scripts" / "agent-autostart.vbs").read_text(encoding="utf-8")
    assert "agent.alive" in text and "agent.log" in text
    assert re.search(r"Const STALE_MIN = 30", text)
    assert "Terminate" in text and "autostart: loop stale" in text
    assert "WScript.Quit 0" in text                                # still exits at once when fresh
