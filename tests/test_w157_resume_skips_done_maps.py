"""W157 (CRM T1047): a `discovery_pending` resume does not re-walk Maps when the job's Maps lane had
already completed — MAC #22556 / #22562 spent ~10 min each on 33 empty tiles after the stop-all re-queue."""
from __future__ import annotations

from webscraper import agent as A


def _claimed(disc_status, pending=True, reason=None):
    return {"id": 1, "discovery_pending": pending,
            "progress": {"lanes": [{"key": "discovery", "status": disc_status, "reason": reason, "done": 189, "total": 189},
                                   {"key": "whatsapp", "status": "stopped", "pending": 16}]}}


def test_completed_maps_is_not_resumed():
    assert A._maps_done_before(_claimed("done"))
    assert A._maps_done_before(_claimed("stopped", reason="completed"))
    assert A._discovery_pending_for(_claimed("done")) is False
    assert A._discovery_pending_for(_claimed("completed")) is False


def test_capped_or_unknown_maps_still_resumes():
    assert A._discovery_pending_for(_claimed("stopped", reason="maps_cap")) is True
    assert A._discovery_pending_for(_claimed("running")) is True
    assert A._discovery_pending_for({"id": 2, "discovery_pending": True}) is True       # no lanes: as asked
    assert A._discovery_pending_for({"id": 3, "discovery_pending": False, "progress": {"lanes": []}}) is False
    assert A._discovery_pending_for(_claimed("done", pending=False)) is False
