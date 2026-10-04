"""W138 (CRM T1016, owner: "different lanes of different jobs together to maximise productivity").

  * the Worker starts a job that needs no Maps (re-enrich pass) while another job's discovery
    runs — `job_needs_discovery` is the rule, `capacity()` tells the CRM via `lanes_free`
  * the enrichment lane crawls alongside WhatsApp / Maps when nobody else is queued for the
    websites slot (W76 becomes a priority, not a block)
  * with another job queued for the slot it still hands the slot over first (W76 kept)
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from webscraper import lanes as L, server, wa_verify
from webscraper.store import Store, now_iso


def test_job_needs_discovery_rule():
    assert server.job_needs_discovery({"reenrich_only": 0, "discovery_pending": 0}) is True
    assert server.job_needs_discovery({"reenrich_only": 1, "discovery_pending": 0}) is False
    assert server.job_needs_discovery({"reenrich_only": 1, "discovery_pending": 1}) is True
    assert server.job_needs_discovery({}) is True                  # unknown shape: assume Maps


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "test.db"
    return lambda: Store(path)


def _job_with_number(new_store, **over):
    s = new_store()
    job_id = s.create_job(query="q", location="here", max_places=10, delay_sec=0)
    s.conn.execute("INSERT INTO places(job_id, place_key, name, phone, enrich_status, scraped_at) VALUES (?,?,?,?,'done',?)",
                   (job_id, "p1", "biz", "+919999999991", now_iso()))
    s.conn.commit()
    s.add_wa_account("acc1")
    s.set_wa_status("acc1", "logged_in")
    s.close()
    job = {"do_enrich": 1, "do_research": 0, "do_wa_verify": 1, "country": "IN", "reenrich_only": 1}
    job.update(over)
    return job_id, job


def _slow_wa(monkeypatch, delay: float, ended: dict):
    monkeypatch.setattr(wa_verify, "login_in_progress", lambda name=None: False)

    def fake_verify(store, batch, on_progress, should_stop, job_id=None, headless=None, account=None):
        time.sleep(delay)
        for r in batch:
            store.record_wa_check(job_id, r["place_key"], r["number"], "maps", "yes")
            on_progress(r["place_key"], "yes", r["number"], "maps")
        ended["wa"] = time.monotonic()
        return {"yes": len(batch), "no": 0, "unknown": 0}
    monkeypatch.setattr(wa_verify, "verify_places", fake_verify)


def test_websites_crawl_alongside_whatsapp_when_the_slot_is_free(db, monkeypatch):
    monkeypatch.setenv("LANE_SLOTS_ENRICHMENT", "1")
    L.reset_stage_gates()
    monkeypatch.setattr(L, "IDLE_POLL_SEC", 0.01)
    ended: dict = {}
    _slow_wa(monkeypatch, 0.6, ended)
    job_id, job = _job_with_number(db)
    pipe = L.Pipeline(job_id, job, lambda lane: L.R_COMPLETED, store_factory=db)
    pipe.run()
    assert pipe.enrichment.reason in (L.R_NO_TARGETS, L.R_COMPLETED)
    assert pipe.whatsapp.reason == L.R_COMPLETED
    logs = [r["message"] for r in db().conn.execute("SELECT message FROM job_logs WHERE job_id=?", (job_id,))]
    assert any("crawling websites alongside" in m for m in logs), logs
    assert not any(m.startswith("websites wait") for m in logs)


def test_websites_still_wait_when_another_job_needs_the_slot(db, monkeypatch):
    """Deterministic: 998 holds the slot, our lane queues behind it, 999 queues behind us.
    998 lets go -> our lane holds while 999 waits -> W76 kept: it parks (W136) and 999 gets
    the slot while WhatsApp is still busy; once 999 is done our lane crawls alongside."""
    monkeypatch.setenv("LANE_SLOTS_ENRICHMENT", "1")
    L.reset_stage_gates()
    monkeypatch.setattr(L.StageGate, "POLL_SEC", 0.005)
    monkeypatch.setattr(L, "IDLE_POLL_SEC", 0.01)
    ended: dict = {}
    _slow_wa(monkeypatch, 1.0, ended)
    job_id, job = _job_with_number(db)
    pipe = L.Pipeline(job_id, job, lambda lane: L.R_COMPLETED, store_factory=db)
    gate = L.STAGE_GATES["enrichment"]
    assert gate.acquire(998, lambda: False, lambda m: None)
    got = {"at": None}

    def other_job():
        for _ in range(600):
            if job_id in gate._queue:
                break
            time.sleep(0.005)
        assert gate.acquire(999, lambda: False, lambda m: None)
        got["at"] = time.monotonic()
        time.sleep(0.05)
        gate.release(999)
    t = threading.Thread(target=other_job, daemon=True)
    t.start()
    runner = threading.Thread(target=pipe.run, daemon=True)
    runner.start()
    for _ in range(600):
        if 999 in gate._queue:
            break
        time.sleep(0.005)
    assert 999 in gate._queue
    gate.release(998)
    runner.join(8)
    t.join(3)
    assert not runner.is_alive()
    assert got["at"] is not None and got["at"] < ended["wa"], "the other job must get the websites slot before WhatsApp finished"
    logs = [r["message"] for r in db().conn.execute("SELECT message FROM job_logs WHERE job_id=?", (job_id,))]
    assert any(m.startswith("websites wait") for m in logs), logs
    assert any("handing the enrichment slot to the next job in line meanwhile" in m for m in logs)
