"""W155 (CRM T1047): a CLAIMED lane-only cloud job whose next lane is held by another job is handed
back to the CRM after LANE_BUSY_RELEASE_SEC (one job per lane, W153) instead of sitting unstarted."""
from __future__ import annotations

from webscraper import server as S


def test_release_rule(monkeypatch):
    monkeypatch.delenv("LANE_ONE_JOB_PER_LANE", raising=False)      # rule ON (conftest turns it off)
    due = S.lane_busy_release_due
    assert due(S.LANE_WHATSAPP, True, False, 31)
    assert due(S.LANE_ENRICHMENT, True, False, 31)
    assert not due(S.LANE_WHATSAPP, True, False, 10), "grace: the slot may free within seconds"
    assert not due(S.LANE_WHATSAPP, False, False, 999), "a local job is not the CRM's to re-route"
    assert not due(S.LANE_WHATSAPP, True, True, 999), "a job that still needs Maps is not lane-only"
    assert not due(S.LANE_DISCOVERY, True, False, 999) and not due(None, True, False, 999)
    monkeypatch.setenv("LANE_ONE_JOB_PER_LANE", "0")
    assert not due(S.LANE_WHATSAPP, True, False, 999), "old W144 behaviour: wait on the gate"
    assert S.LANE_BUSY_RELEASE_SEC == 30.0
