"""W201 (CRM T1451, 2026-10-27): `3 - ASUS` / `5 - MI` polled the CRM for 30+ min with 40 / 28 jobs
pinned to them and logged NOTHING after their startup banner — the CRM withholds a job it reads as
WhatsApp-next (from the previous run's `progress.lanes`) from any machine whose `wa_free` is false,
which is every machine with `lane_pause__<device> = whatsapp`. The loop was never hung (heartbeat and
self-check kept arriving), so nothing in the agent ever said a word. It must say it, loudly."""
import logging
import time

from webscraper import agent


def test_starve_due_is_pure_and_rate_limited():
    now = 10_000.0
    # never before the first answer, and never with the warning switched off
    assert agent._starve_due(now, 0.0, 0.0, 900.0) is False
    assert agent._starve_due(now, now - 3600, 0.0, 0.0) is False
    # fresh offer -> quiet; stale offer -> warn once per limit
    assert agent._starve_due(now, now - 100, 0.0, 900.0) is False
    assert agent._starve_due(now, now - 1000, 0.0, 900.0) is True
    assert agent._starve_due(now, now - 1000, now - 10, 900.0) is False
    assert agent._starve_due(now, now - 2000, now - 950, 900.0) is True


class _Worker:
    current_job = None

    def __init__(self, cap):
        self._cap = cap

    def inflight_jobs(self):
        return []

    def capacity(self):
        return self._cap


class _Srv:
    def __init__(self, worker):
        self.worker = worker


def test_warns_with_the_lane_flags_when_the_crm_offers_nothing(caplog, monkeypatch):
    monkeypatch.setattr(agent, "IDLE_STARVE_SEC", 900.0)
    agent._IDLE_WD["offer"] = time.monotonic() - 1800
    agent._IDLE_WD["warn"] = 0.0
    srv = _Srv(_Worker({"maps_free": True, "enrich_free": True, "wa_free": False,
                        "lanes_paused": ["whatsapp"], "inflight": 0, "max_inflight": 8}))
    with caplog.at_level(logging.WARNING, logger="webscraper.agent"):
        agent._warn_if_starved(srv)
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "offered this machine no job" in msg
    assert "wa_free=False" in msg and "whatsapp" in msg
    # rate limited: a second call right away says nothing more
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="webscraper.agent"):
        agent._warn_if_starved(srv)
    assert not caplog.records


def test_a_working_machine_is_never_warned(caplog, monkeypatch):
    monkeypatch.setattr(agent, "IDLE_STARVE_SEC", 900.0)
    agent._IDLE_WD["offer"] = time.monotonic() - 1800
    agent._IDLE_WD["warn"] = 0.0
    w = _Worker({})
    w.current_job = 7
    with caplog.at_level(logging.WARNING, logger="webscraper.agent"):
        agent._warn_if_starved(_Srv(w))
    assert not caplog.records
    # and the clock is re-armed, so the first idle minute after a job is not counted as starvation
    assert time.monotonic() - agent._IDLE_WD["offer"] < 5
