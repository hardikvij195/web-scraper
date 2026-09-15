"""W115 (CRM T646):

1. `_cloud_retry` — a stream/progress call to the CRM retries a transient
   `httpx.HTTPError` ("Server disconnected without sending a response", a read timeout, a
   `getaddrinfo failed` DNS blip) with backoff before the caller's own "give up, retry next
   tick" fallback ever sees it.
2. Self-update: yesterday's deferred updates ran, but nothing ever asked a machine to check
   for itself, so the fleet stayed on 1.9.3 while 1.9.4 was already out. `_maybe_self_update`
   now queues the SAME deferred `update` W100 already runs at a job boundary, when
   origin/main's VERSION is ahead of this machine's own — opt-in, default on, never while a
   job runs (it only ever sets `_DEFERRED_CMD`, which `_run_deferred` already refuses to run
   with a job in flight).
"""
from __future__ import annotations

import httpx
import pytest

from webscraper import agent


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)


def test_cloud_retry_recovers_after_transient_failures():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadTimeout("The read operation timed out")
        return "ok"

    assert agent._cloud_retry(flaky, "test call") == "ok"
    assert calls["n"] == 3


def test_cloud_retry_raises_once_every_attempt_is_spent():
    def always_fails():
        raise httpx.ConnectError("getaddrinfo failed")

    with pytest.raises(httpx.ConnectError):
        agent._cloud_retry(always_fails, "test call")


def test_cloud_retry_does_not_swallow_the_final_error_type():
    def disconnects():
        raise httpx.RemoteProtocolError("Server disconnected without sending a response")

    with pytest.raises(httpx.RemoteProtocolError):
        agent._cloud_retry(disconnects, "test call")


# ── self-update ──────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clean_self_update_state(monkeypatch):
    monkeypatch.setattr(agent, "_DEFERRED_CMD", [None])
    monkeypatch.setattr(agent, "_last_self_update_check", [0.0])
    monkeypatch.setenv("WEBSCRAPER_AUTO_UPDATE", "1")


def test_self_update_queues_the_same_deferred_command_when_remote_is_newer(tmp_path, monkeypatch):
    (tmp_path / "VERSION").write_text("1.9.4\n", encoding="utf-8")
    import webscraper.config as config_mod
    monkeypatch.setattr(config_mod, "ROOT", tmp_path)
    monkeypatch.setattr(agent, "_remote_version", lambda: "1.9.5")

    agent._maybe_self_update(cloud=None, force=True)

    assert agent._DEFERRED_CMD[0] == "update"


def test_self_update_does_nothing_when_already_current(tmp_path, monkeypatch):
    (tmp_path / "VERSION").write_text("1.9.5\n", encoding="utf-8")
    import webscraper.config as config_mod
    monkeypatch.setattr(config_mod, "ROOT", tmp_path)
    monkeypatch.setattr(agent, "_remote_version", lambda: "1.9.5")

    agent._maybe_self_update(cloud=None, force=True)

    assert agent._DEFERRED_CMD[0] is None


def test_self_update_respects_the_opt_out(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSCRAPER_AUTO_UPDATE", "0")
    (tmp_path / "VERSION").write_text("1.9.4\n", encoding="utf-8")
    import webscraper.config as config_mod
    monkeypatch.setattr(config_mod, "ROOT", tmp_path)
    monkeypatch.setattr(agent, "_remote_version", lambda: "1.9.5")

    agent._maybe_self_update(cloud=None, force=True)

    assert agent._DEFERRED_CMD[0] is None


def test_self_update_never_overrides_a_pending_command(tmp_path, monkeypatch):
    (tmp_path / "VERSION").write_text("1.9.4\n", encoding="utf-8")
    import webscraper.config as config_mod
    monkeypatch.setattr(config_mod, "ROOT", tmp_path)
    monkeypatch.setattr(agent, "_remote_version", lambda: "1.9.5")
    agent._DEFERRED_CMD[0] = "restart"

    agent._maybe_self_update(cloud=None, force=True)

    assert agent._DEFERRED_CMD[0] == "restart"


def test_self_update_throttles_idle_checks_to_the_interval(tmp_path, monkeypatch):
    (tmp_path / "VERSION").write_text("1.9.4\n", encoding="utf-8")
    import webscraper.config as config_mod
    monkeypatch.setattr(config_mod, "ROOT", tmp_path)
    calls = {"n": 0}
    clock = {"t": 10_000.0}   # a fake, controlled monotonic clock — real uptime is not safe here
    monkeypatch.setattr(agent.time, "monotonic", lambda: clock["t"])

    def remote():
        calls["n"] += 1
        return "1.9.5"
    monkeypatch.setattr(agent, "_remote_version", remote)

    agent._maybe_self_update(cloud=None, force=False)      # far past the interval since 0.0 — runs
    agent._DEFERRED_CMD[0] = None            # pretend it wasn't queued, to isolate throttling
    clock["t"] += 5.0                        # 5s later — well inside the 30-min window
    agent._maybe_self_update(cloud=None, force=False)

    assert calls["n"] == 1   # the second call was inside the 30-min window — no fetch
