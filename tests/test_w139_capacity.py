"""W139 (CRM T1016): a job claimed from the CRM but not started yet counts as in flight in
`Worker.capacity()` — one poll per 5 s used to claim one more job each time until the local
queue held 5 for a max_inflight of 3 (ASUS / DELL, 2026-10-04 21:20 IST)."""
from __future__ import annotations

from webscraper import server as server_mod
from webscraper.store import Store


def test_claimed_but_unstarted_cloud_jobs_count_as_inflight(tmp_path, monkeypatch):
    db_path = tmp_path / "w139.db"
    monkeypatch.setattr(server_mod, "Store", lambda *a, **kw: Store(db_path))
    monkeypatch.setattr(server_mod, "max_inflight_jobs", lambda: 3)
    monkeypatch.setattr(server_mod, "memory_blocked", lambda: False)
    s = Store(db_path)
    a = s.create_job(query="a", location="x", max_places=5, delay_sec=0, phase="queued")
    b = s.create_job(query="b", location="x", max_places=5, delay_sec=0, phase="queued")
    c = s.create_job(query="c", location="x", max_places=5, delay_sec=0, phase="queued")      # local, not from the CRM
    s.update_job(a, cloud_id=101, cloud_kind="crm")
    s.update_job(b, cloud_id=102, cloud_kind="crm")
    s.close()

    w = server_mod.Worker()
    cap = w.capacity()
    assert cap["started"] == 0
    assert cap["inflight"] == 2                       # a + b: claimed, mirrored, waiting to start
    assert cap["lanes_free"] is True                  # 2 < 3

    w._inflight[a] = None                             # a started: counted once, not twice
    w._disc_job = a
    cap = w.capacity()
    assert cap["started"] == 1 and cap["inflight"] == 2
    assert cap["discovery_free"] is False

    s = Store(db_path)
    d = s.create_job(query="d", location="x", max_places=5, delay_sec=0, phase="queued")
    s.update_job(d, cloud_id=103, cloud_kind="crm")
    s.close()
    cap = w.capacity()
    assert cap["inflight"] == 3 and cap["lanes_free"] is False   # at max: the CRM must offer nothing more
