"""W214 (CRM T1555): remote_setup / remote_on / remote_off / claude_on / claude_off reach the
platform script through the agent's command poller, and the RustDesk password never comes back."""
from __future__ import annotations

import subprocess

import pytest

from webscraper import agent, remote_access as ra


class FakeCloud:
    def __init__(self, cmd):
        self.cmd, self.done, self._checks_sent_at = cmd, [], 1.0

    def command(self):
        c, self.cmd = self.cmd, None
        return c

    def command_done(self, cid, ok, result=None):
        self.done.append((cid, ok, result))


def _poll(cloud):
    agent._cmd_thread = None
    agent._poll_command(cloud, force=True)
    agent._cmd_thread.join(10)
    return cloud.done


@pytest.mark.parametrize("name", ra.COMMANDS)
def test_agent_dispatches_every_remote_command(monkeypatch, name):
    seen = []
    monkeypatch.setattr(ra, "run", lambda c, a, device="": (seen.append((c, a, device)) or (True, f"{c} ok")))
    monkeypatch.setattr(agent, "_CRM_LOG", [])
    done = _poll(FakeCloud({"id": 7, "command": name, "arg": "s3cretPass" if name == "remote_setup" else None}))
    assert done == [(7, True, f"{name} ok")]
    assert seen[0][0] == name and seen[0][2] == agent.DEVICE_NAME


def _proc(rc, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


def test_password_goes_in_env_not_argv_and_is_redacted():
    calls = {}

    def runner(argv, **kw):
        calls["argv"], calls["env"] = argv, kw["env"]
        return _proc(0, "noise\nRESULT: RustDesk installed\nRESULT: echoed s3cretPass9 oops\n")

    ok, res = ra.run("remote_setup", "  s3cretPass9 ", device="4 - DELL", platform="win32", runner=runner)
    assert ok
    assert "s3cretPass9" not in " ".join(calls["argv"]) and "s3cretPass9" not in res
    assert calls["env"]["HVT_RD_PASSWORD"] == "s3cretPass9" and calls["env"]["HVT_DEVICE"] == "4 - DELL"
    assert res == "RustDesk installed; echoed *** oops"
    assert calls["argv"][-1] == "remote_setup" and calls["argv"][-2].endswith("windows.ps1")


def test_setup_refuses_short_or_missing_password():
    for arg in (None, "", "short"):
        ok, res = ra.run("remote_setup", arg, platform="darwin", runner=lambda *a, **k: pytest.fail("ran"))
        assert not ok and "password" in res


def test_other_commands_get_no_password_and_mac_script():
    calls = {}

    def runner(argv, **kw):
        calls["argv"], calls["env"] = argv, kw["env"]
        return _proc(1, "RESULT: RustDesk still running", "boom")

    ok, res = ra.run("remote_off", "ignored-arg", platform="darwin", runner=runner)
    assert not ok and res == "RustDesk still running"
    assert calls["env"]["HVT_RD_PASSWORD"] == "" and calls["argv"][:2] == ["/bin/bash", str(ra.SCRIPTS / "mac.sh")]


def test_timeout_unknown_and_platform():
    def slow(*a, **k):
        raise subprocess.TimeoutExpired("x", 1)
    assert ra.run("claude_on", None, platform="win32", runner=slow) == (False, "claude_on timed out on this machine")
    assert ra.run("rm_rf", None)[0] is False
    assert ra.run("remote_on", None, platform="linux")[0] is False
    assert ra.TIMEOUT_SEC["remote_setup"] < agent.CMD_MAX_SEC
    assert (ra.SCRIPTS / "mac.sh").exists() and (ra.SCRIPTS / "windows.ps1").exists()
