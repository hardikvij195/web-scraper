"""W178 (DELL 2026-10-07 11:51Z): `update` pulled 61b6193 -> feea96b, logged "restarting",
then the command_done POST died on a DNS blip (getaddrinfo failed) and the exception
skipped the self-restart — the agent ran the OLD code for 20 min and the CRM showed
`cmd update/running`. The restart must happen whether or not the CRM ack lands."""
import logging
import subprocess
import types

import pytest

from webscraper import agent


class _Done(types.SimpleNamespace):
    pass


def _fake_run(seq):
    """subprocess.run stand-in: every git/pip step succeeds; `rev-parse --short HEAD`
    answers the old sha first, the new one after."""
    def run(args, **kw):
        if args[:3] == ["git", "rev-parse", "--short"]:
            seq.append(1)
            return _Done(returncode=0, stdout="61b6193\n" if len(seq) == 1 else "feea96b\n", stderr="")
        if args[:2] == ["git", "pull"]:
            return _Done(returncode=0, stdout="Updating 61b6193..feea96b\n Fast-forward\n", stderr="")
        return _Done(returncode=0, stdout="", stderr="")
    return run


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run([]))
    monkeypatch.setattr(agent, "ACK_RETRY_SEC", 0.0)
    restarts: list = []
    monkeypatch.setattr(agent, "_exit_for_restart", lambda cloud: restarts.append(cloud))
    return restarts


def test_update_restarts_even_when_the_crm_ack_fails(harness, caplog):
    acks: list = []

    class Cloud:
        def command_done(self, cid, ok, result=None):
            acks.append((cid, ok, result))
            raise OSError("[Errno 11001] getaddrinfo failed")

    cloud = Cloud()
    with caplog.at_level(logging.WARNING, logger=agent.log.name):
        ok, result = agent._do_update(cloud, 4242)

    assert ok is True and result == "updated 61b6193 → feea96b, restarting"
    assert harness == [cloud], "the self-restart must run despite the failed ack"
    assert len(acks) == 2, "command_done is retried exactly once after the first failure"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("restarting anyway, the next heartbeat carries the new version" in m for m in msgs), msgs
    assert any("retrying once" in m for m in msgs), msgs


def test_update_restarts_exactly_once_when_the_ack_succeeds(harness):
    acks: list = []

    class Cloud:
        def command_done(self, cid, ok, result=None):
            acks.append((cid, ok, result))

    cloud = Cloud()
    ok, result = agent._do_update(cloud, 7)

    assert ok is True
    assert acks == [(7, True, "updated 61b6193 → feea96b, restarting")]
    assert harness == [cloud], "restart invoked exactly once"


def test_restart_command_restarts_even_when_the_crm_ack_fails(harness, caplog):
    class Cloud:
        def command_done(self, cid, ok, result=None):
            raise OSError("getaddrinfo failed")

    cloud = Cloud()
    with caplog.at_level(logging.WARNING, logger=agent.log.name):
        agent._do_restart(cloud, 9)
    assert harness == [cloud]
    assert any("restarting anyway" in r.getMessage() for r in caplog.records)
