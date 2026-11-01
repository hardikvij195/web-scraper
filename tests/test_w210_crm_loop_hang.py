"""W210/T1491 — the CRM loop "hung" ~610 s on every big re-enrich hand-over (MAC #6629 4x in 1 h,
ASUS 36 min silent). Hydrated rows were streamed straight back to the CRM, failed CRM calls never
stamped the watchdog's `err`, retries had no total budget and the watchdog's exit path could block.
"""
from __future__ import annotations

import threading
from pathlib import Path

import httpx
import pytest

from webscraper import agent
from webscraper.store import Store


@pytest.fixture()
def store(tmp_path: Path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


class _FakeCloud:
    def __init__(self, rows):
        self._rows = rows

    def results(self, jid):
        return self._rows


def _rows(n):
    return [{"place_key": f"p{i}", "name": f"P{i}", "website": f"https://p{i}.example",
             "enrich_status": "pending", "enrich_error": "http_403" if i % 2 else None}
            for i in range(n)]


def test_hydrated_rows_are_not_streamed_back(store: Store):
    jid = store.create_job(query="q", location="here", max_places=10, delay_sec=0)
    agent._hydrate_enrichment_from_cloud(_FakeCloud(_rows(5)), store, jid, cloud_job_id=1)
    upto, top = agent._HYDRATED.pop(jid)
    fresh, _ = store.places_after(jid, upto)
    assert fresh == []                                  # nothing new to send
    changed, _ = store.places_changed_since(jid, top, upto)
    assert changed == []                                # the enrich_error UPDATE bump is not "news"
    # a lane writing a verdict afterwards IS streamed
    store.conn.execute("UPDATE places SET enrich_status='done', changed_at='9999' WHERE job_id=? "
                       "AND place_key='p0'", (jid,))
    store.conn.commit()
    changed, _ = store.places_changed_since(jid, top, upto)
    assert [c["place_key"] for c in changed] == ["p0"]


def test_watermark_stops_below_a_local_row_the_crm_lacks(store: Store):
    from webscraper.models import Place
    jid = store.create_job(query="q", location="here", max_places=10, delay_sec=0)
    store.upsert_place(Place(job_id=jid, place_key="local-only", name="L"))
    agent._hydrate_enrichment_from_cloud(_FakeCloud(_rows(3)), store, jid, cloud_job_id=1)
    upto, _ = agent._HYDRATED.pop(jid)
    fresh, _ = store.places_after(jid, upto)
    assert "local-only" in {f["place_key"] for f in fresh}


def test_cloud_retry_failures_stamp_err_and_respect_budget(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(agent.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(agent.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(agent, "_touch_alive", lambda: None)
    calls = []

    def boom():
        calls.append(1)
        clock[0] += 60.0                                # every attempt eats a full read timeout
        raise httpx.ReadTimeout("slow")

    agent._LOOP_WD["err"] = 0.0
    with pytest.raises(httpx.ReadTimeout):
        agent._cloud_retry(boom, "test")
    assert len(calls) == 2                              # 60 + 2 + 60 > 90 s budget: no 3rd/4th try
    assert agent._LOOP_WD["err"] > 0.0                  # the watchdog knows the loop is alive


def test_watchdog_restart_exits_even_if_cleanup_blocks(monkeypatch):
    monkeypatch.setattr(agent, "WATCHDOG_CLEANUP_SEC", 0.2)
    block = threading.Event()
    monkeypatch.setattr(agent, "_watchdog_cleanup", lambda s: block.wait(30))
    exited = []

    def fake_exit(code):
        exited.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(agent.os, "_exit", fake_exit)
    with pytest.raises(SystemExit):
        agent._watchdog_restart(610)
    block.set()
    assert exited == [3]


def test_crm_client_has_finite_connect_and_pool_timeouts():
    t = agent.CRM_HTTP_TIMEOUT
    assert t.read == 60.0 and t.connect == 15.0 and t.pool == 20.0
