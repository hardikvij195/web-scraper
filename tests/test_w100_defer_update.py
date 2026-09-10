"""W100 (CRM T538): an `update` / `restart` that arrives while a job is running is parked
behind the job — acknowledged to the CRM as done ("deferred — …"), remembered in
`_DEFERRED_CMD`, and executed by `_run_deferred` from the main loop once the worker frees.

Nothing here touches the network, `data/leads.db` or the real update path: `_cloud_job_id`,
`_do_update` and `_do_restart` are monkeypatched (the real ones end in `os._exit`).
"""
from __future__ import annotations

import pytest

from webscraper import agent


@pytest.fixture(autouse=True)
def _clean_slot(monkeypatch):
    """Every test starts idle with an empty deferred slot and no command thread."""
    agent._DEFERRED_CMD[0] = None
    monkeypatch.setattr(agent, "_cmd_thread", None)
    monkeypatch.setattr(agent.srv.worker, "current_job", None)
    yield
    agent._DEFERRED_CMD[0] = None


class _FakeCloud:
    def __init__(self, cmd: dict | None = None):
        self._cmd = cmd
        self.done: list[tuple] = []
        self._checks_sent_at = 1.0

    def command(self):
        c, self._cmd = self._cmd, None
        return c

    def command_done(self, cid: int, ok: bool, result: str | None = None) -> None:
        self.done.append((cid, ok, result))


def test_should_defer_only_exit_commands_while_a_job_runs():
    assert agent._should_defer({"command": "update"}, 53) is True
    assert agent._should_defer({"command": "restart"}, 53) is True
    # idle machine: run it now, exactly as before
    assert agent._should_defer({"command": "update"}, None) is False
    assert agent._should_defer({"command": "restart"}, None) is False
    # everything else is untouched, busy or not
    for name in ("stop", "start", "checks", "wa_login", "wa_reset", "wa_rename", "rename",
                 "verify_folder", "relocate"):
        assert agent._should_defer({"command": name}, 53) is False
    assert agent._should_defer({}, 53) is False


def test_defer_command_reports_done_with_the_cloud_job_id(monkeypatch):
    monkeypatch.setattr(agent, "_cloud_job_id", lambda local_id: 1618)
    ok, result = agent._defer_command({"id": 7, "command": "update"}, 53)
    assert ok is True
    assert result == "deferred — will update after job #1618 finishes"
    assert agent._DEFERRED_CMD[0] == "update"


def test_defer_command_falls_back_to_the_local_id(monkeypatch):
    monkeypatch.setattr(agent, "_cloud_job_id", lambda local_id: None)
    ok, result = agent._defer_command({"id": 8, "command": "restart"}, 53)
    assert ok is True
    assert result == "deferred — will restart after job local #53 finishes"
    assert agent._DEFERRED_CMD[0] == "restart"


def test_run_deferred_waits_for_the_job_then_runs_update_once(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(agent, "_do_update", lambda cloud, cmd_id: calls.append(("update", cmd_id)) or (True, "x"))
    monkeypatch.setattr(agent, "_do_restart", lambda cloud, cmd_id: calls.append(("restart", cmd_id)))
    cloud = _FakeCloud()

    # nothing parked: no-op
    assert agent._run_deferred(cloud) is False

    agent._DEFERRED_CMD[0] = "update"
    # job still running: keep waiting, slot intact
    monkeypatch.setattr(agent.srv.worker, "current_job", 53)
    assert agent._run_deferred(cloud) is False
    assert calls == [] and agent._DEFERRED_CMD[0] == "update"

    # worker freed: run it, with no command id (the CRM command was closed as deferred)
    monkeypatch.setattr(agent.srv.worker, "current_job", None)
    assert agent._run_deferred(cloud) is True
    assert calls == [("update", None)]
    assert agent._DEFERRED_CMD[0] is None
    # never twice
    assert agent._run_deferred(cloud) is False
    assert calls == [("update", None)]
    assert cloud.done == []          # nothing re-reported to the CRM


def test_run_deferred_restart_and_busy_command_slot(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(agent, "_do_update", lambda cloud, cmd_id: calls.append(("update", cmd_id)) or (True, "x"))
    monkeypatch.setattr(agent, "_do_restart", lambda cloud, cmd_id: calls.append(("restart", cmd_id)))
    cloud = _FakeCloud()
    agent._DEFERRED_CMD[0] = "restart"

    class _Alive:
        def is_alive(self):
            return True

    # a wa_login (or any command) still owns the slot: do not kill it
    monkeypatch.setattr(agent, "_cmd_thread", _Alive())
    assert agent._run_deferred(cloud) is False
    assert calls == [] and agent._DEFERRED_CMD[0] == "restart"

    monkeypatch.setattr(agent, "_cmd_thread", None)
    assert agent._run_deferred(cloud) is True
    assert calls == [("restart", None)]
    assert agent._DEFERRED_CMD[0] is None


def test_run_deferred_failed_update_is_reported_once_and_dropped(monkeypatch):
    monkeypatch.setattr(agent, "_do_update", lambda cloud, cmd_id: (False, "git pull failed: offline"))
    agent._DEFERRED_CMD[0] = "update"
    assert agent._run_deferred(_FakeCloud()) is True
    assert agent._DEFERRED_CMD[0] is None          # not retried on its own


def test_command_loop_defers_update_while_a_job_runs(monkeypatch):
    """End to end through `_poll_command`: the CRM's command is closed as done with the
    deferred result, the real update path is never entered, the slot holds the name."""
    ran: list = []
    monkeypatch.setattr(agent, "_do_update", lambda cloud, cmd_id: ran.append(cmd_id) or (True, "x"))
    monkeypatch.setattr(agent, "_cloud_job_id", lambda local_id: 1618)
    monkeypatch.setattr(agent.srv.worker, "current_job", 53)
    cloud = _FakeCloud({"id": 7, "command": "update"})

    agent._poll_command(cloud)
    t = agent._cmd_thread
    assert t is not None
    t.join(10)
    assert not t.is_alive()

    assert ran == []
    assert cloud.done == [(7, True, "deferred — will update after job #1618 finishes")]
    assert agent._DEFERRED_CMD[0] == "update"


def test_command_loop_runs_update_now_when_idle(monkeypatch):
    ran: list = []
    monkeypatch.setattr(agent, "_do_update", lambda cloud, cmd_id: ran.append(cmd_id) or (True, "updated"))
    cloud = _FakeCloud({"id": 9, "command": "update"})

    agent._poll_command(cloud)
    agent._cmd_thread.join(10)

    assert ran == [9]
    assert agent._DEFERRED_CMD[0] is None
    # the stub returned instead of exiting, so the loop's `finally` reported it
    assert cloud.done == [(9, True, "updated")]


def test_stop_flags_the_running_job_in_its_own_store(monkeypatch, tmp_path):
    """W101: the parked Stop writes stop_requested on the running local job. The branch
    used to reference a `store` that does not exist in the command thread (NameError,
    swallowed), so the lanes only stopped once the CRM's cancel came back."""
    from webscraper.config import settings
    from webscraper.store import Store

    monkeypatch.setattr(settings, "db_path", tmp_path / "stop.db")
    monkeypatch.setattr(agent, "_close_browsers", lambda *a, **k: None)
    s = Store()
    jid = s.create_job(query="dentist", location="Pune", max_places=10, delay_sec=0)
    s.close()
    monkeypatch.setattr(agent.srv.worker, "current_job", jid)
    agent._STANDBY[0] = False
    cloud = _FakeCloud({"id": 11, "command": "stop"})
    try:
        agent._poll_command(cloud)
        agent._cmd_thread.join(10)
    finally:
        agent._STANDBY[0] = False

    s = Store()
    try:
        assert s.stop_requested(jid) is True
        row = s.get_job(jid)
        assert "parked from the CRM" in (row["message"] or "")
    finally:
        s.close()
    assert cloud.done == [(11, True, "agent stopped — parked, press Start in the CRM to run it again")]
