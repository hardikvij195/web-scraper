"""W192 (CRM T1320): a re-verify thread that hangs after its work is over never blocks claims.

2 - MAC, 2026-10-20: #22581 crashed at 06:12 IST, its crash report reached the CRM, yet the
`reverify-22581` thread never ended and `_reverify_busy()` kept returning 22581 — no job claimed
for 6 h. Fakes only: no Playwright, no Chrome, no CRM.
"""
from __future__ import annotations

import threading
import time

import pytest

from webscraper import agent
from webscraper.lanes import STAGE_GATES


class HangingCloud:
    """`done` blocks until released — stands in for whatever the crash cleanup hung on."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.done_calls: list[tuple] = []

    def progress(self, *a, **k) -> None:
        pass

    def done(self, jid, status, msg) -> None:
        self.done_calls.append((jid, status))
        self.release.wait(10)


@pytest.fixture(autouse=True)
def _reset():
    agent._REVERIFY.update(thread=None, job=None, stop=None, why=None, ended=False, stop_at=None)
    yield
    agent._REVERIFY.update(thread=None, job=None, stop=None, why=None, ended=False, stop_at=None)


def _wait(pred, sec: float = 5.0) -> bool:
    end = time.monotonic() + sec
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_crashed_reverify_with_hung_cleanup_is_not_busy(monkeypatch):
    cloud = HangingCloud()

    def boom(*a, **k):
        raise RuntimeError("final set_wa failed: The read operation timed out")

    monkeypatch.setattr(agent, "_reverify_wa", boom)
    monkeypatch.setattr(agent, "Store", lambda: type("S", (), {"close": lambda self: None})())
    agent._start_reverify(cloud, 22581, False)
    assert _wait(lambda: cloud.done_calls), "crash report never sent"
    t = agent._REVERIFY["thread"]
    assert t is not None and t.is_alive()            # still blocked in the crash report
    assert agent._reverify_busy() is None            # ...but the claim loop may move on
    gate = STAGE_GATES.get("whatsapp")
    if gate is not None:
        assert not gate.holds(-22581)                # the WhatsApp slot is free for the next job
    assert agent._REVERIFY["job"] is None
    cloud.release.set()
    t.join(5)


def test_running_reverify_is_busy_until_stop_grace(monkeypatch):
    go = threading.Event()
    t = threading.Thread(target=lambda: go.wait(10), daemon=True)
    t.start()
    agent._REVERIFY.update(thread=t, job=777, stop=threading.Event())
    try:
        assert agent._reverify_busy() == 777         # healthy run: busy
        assert agent._stop_reverify("parked") is True
        assert agent._reverify_busy() == 777         # just asked to stop: still busy
        monkeypatch.setenv("REVERIFY_STOP_GRACE_SEC", "0.05")
        time.sleep(0.1)
        assert agent._reverify_busy() is None        # outlived its stop: released
        assert agent._REVERIFY["thread"] is None
    finally:
        go.set()
        t.join(5)
