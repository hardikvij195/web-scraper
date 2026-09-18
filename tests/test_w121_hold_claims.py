"""W121 (CRM T767): while an `update` / `restart` is parked behind a job, `_tick` must not
claim the next queued job — otherwise a full queue keeps the machine busy and
`_run_deferred` never gets its boundary (5 - MI took #6994 on 1.9.9 the moment #6992 was
cancelled, and its 2.0.0 update kept waiting). Fakes only: no network, no real Store writes.
"""
from __future__ import annotations

import pytest

from webscraper import agent


@pytest.fixture(autouse=True)
def _clean_slot(monkeypatch):
    agent._DEFERRED_CMD[0] = None
    monkeypatch.setattr(agent, "_reverify_busy", lambda: None)
    yield
    agent._DEFERRED_CMD[0] = None


class _Conn:
    def execute(self, *_a, **_k):
        return self

    def fetchall(self):
        return []


class _Store:
    conn = _Conn()


class _Cloud:
    def __init__(self):
        self.claimed: list[int] = []

    def jobs(self):
        return [{"id": 6994, "status": "queued", "query": "x"},
                {"id": 6995, "status": "queued", "wa_verify_only": True}]

    def claim(self, jid: int):
        self.claimed.append(jid)
        return None            # "someone else took it" — stops the tick before create_job


def test_tick_claims_nothing_while_an_update_is_parked():
    agent._DEFERRED_CMD[0] = "update"
    cloud = _Cloud()
    agent._tick(cloud, _Store(), "crm")
    assert cloud.claimed == []


def test_tick_claims_again_once_the_slot_is_empty():
    cloud = _Cloud()
    agent._tick(cloud, _Store(), "crm")
    assert cloud.claimed == [6994, 6995]
