"""W151 (CRM T1045): memory pressure on the 8 GB laptops.

ASUS ran 9 jobs at 86-91 % RAM for 20 min (Maps + WhatsApp Chromes + the websites lane's real-Chrome
fallback), then went silent at 18:22 with no error — a machine deep in swap cannot run its own
watchdog. Three defences, each tested here:
  (1) the crash guard can never exceed what the RAM carries (`ram_inflight_cap`, `max_inflight_jobs`);
  (2) sustained RAM above the shed line PARKS the newest lane-only job (`Worker._shed_if_memory_high`);
  (3) the websites lane does not launch its fallback Chrome above 80 % (`browser_fallback_allowed`).
None of them may touch the WMI / sysctl probe on the scheduling path (`last_memory` is cache-only).
"""
import threading

import pytest

from webscraper import server as server_mod
from webscraper.server import (LANE_DISCOVERY, LANE_ENRICHMENT, LANE_WHATSAPP, MEMORY_SHED_PCT,
                               last_memory, max_inflight_jobs, pick_shed_victim, ram_inflight_cap)
from webscraper.store import Store


# ── (1) RAM-based crash guard ─────────────────────────────────────────────────────────────
def test_ram_cap_per_machine_size():
    assert ram_inflight_cap(8365.5) == 5          # ASUS / MAC / MI (8 GB)
    assert ram_inflight_cap(17072.8) == 10        # DELL (16 GB)
    assert ram_inflight_cap(68469.9) == 12        # PC (64 GB): hard ceiling
    assert ram_inflight_cap(None) == 12           # unknown: the configured guard rules
    assert ram_inflight_cap(0) == 12
    assert ram_inflight_cap(2000) == 2            # floor — never below two jobs


def test_configured_guard_is_clamped_by_ram(monkeypatch):
    monkeypatch.delenv("MAX_INFLIGHT_JOBS", raising=False)
    import webscraper.agent as agent_mod
    monkeypatch.setattr(agent_mod, "DEVICE_NAME", "3 - ASUS")
    monkeypatch.setenv("MAX_INFLIGHT__3 - ASUS", "9")
    monkeypatch.setattr(server_mod, "_MEM_CACHE", [0.0, {"total_mb": 8365.5, "used_pct": 60}])
    assert max_inflight_jobs() == 5               # CRM says 9, the 8 GB machine allows 5
    monkeypatch.setenv("MAX_INFLIGHT__3 - ASUS", "4")
    assert max_inflight_jobs() == 4               # a lower CRM setting still wins
    monkeypatch.setattr(server_mod, "_MEM_CACHE", [0.0, None])
    monkeypatch.setenv("MAX_INFLIGHT__3 - ASUS", "9")
    assert max_inflight_jobs() == 9               # no reading yet: the configured guard


def test_guard_reads_the_cache_only_never_the_probe(monkeypatch):
    from webscraper import healthcheck as HC
    calls = []
    monkeypatch.setattr(HC, "_memory", lambda: calls.append(1) or {"total_mb": 8000, "used_pct": 50})
    monkeypatch.setattr(server_mod, "_MEM_CACHE", [0.0, None])
    assert last_memory() is None
    max_inflight_jobs()
    assert calls == []                            # the scheduling path never shells out


# ── (2) memory shedding ───────────────────────────────────────────────────────────────────
def test_shed_victim_rules():
    started = [(1, LANE_DISCOVERY), (2, LANE_WHATSAPP), (3, LANE_ENRICHMENT), (4, LANE_DISCOVERY)]
    assert pick_shed_victim(started) == 3         # newest lane-only job, never the Maps job while one exists
    assert pick_shed_victim([(1, LANE_DISCOVERY), (4, LANE_DISCOVERY)]) == 4
    assert pick_shed_victim([(7, None), (8, LANE_WHATSAPP)]) == 8
    assert pick_shed_victim([(1, LANE_DISCOVERY)]) is None   # the only job is never parked
    assert pick_shed_victim([]) is None


@pytest.fixture()
def shed_worker(tmp_path, monkeypatch):
    db_path = tmp_path / "shed.db"
    monkeypatch.setattr(server_mod, "Store", lambda *a, **kw: Store(db_path))
    w = server_mod.Worker()
    seed = Store(db_path)
    ids = [seed.create_job(query=f"q{i}", location=None, max_places=10, delay_sec=0, phase="scraping") for i in range(3)]
    seed.close()
    with w._lock:
        for jid, lane in zip(ids, (LANE_DISCOVERY, LANE_WHATSAPP, LANE_ENRICHMENT)):
            w._inflight[jid] = None
            w._start_lane[jid] = lane
    clock = {"t": 1000.0}
    monkeypatch.setattr(server_mod.time, "monotonic", lambda: clock["t"])

    def mem(pct):
        monkeypatch.setattr(server_mod, "_MEM_CACHE", [0.0, {"total_mb": 8365.5, "used_pct": pct}])

    def stopped(jid):
        s = Store(db_path)
        try:
            row = s.conn.execute("SELECT stop_requested, message FROM jobs WHERE id=?", (jid,)).fetchone()
            return bool(row["stop_requested"]), row["message"]
        finally:
            s.close()

    return w, ids, clock, mem, stopped


def test_sustained_high_memory_parks_the_newest_lane_only_job(shed_worker):
    w, ids, clock, mem, stopped = shed_worker
    mem(95)
    w._shed_if_memory_high()                      # first sighting only starts the clock
    assert all(not stopped(j)[0] for j in ids)
    clock["t"] += 10
    w._shed_if_memory_high()                      # 10 s: still inside the hold window
    assert all(not stopped(j)[0] for j in ids)
    clock["t"] += 25
    w._shed_if_memory_high()                      # 35 s above the line -> park ONE job
    flags = [stopped(j)[0] for j in ids]
    assert flags == [False, False, True]          # the enrichment job (newest lane-only), not Maps, not WhatsApp
    assert "parked by the memory guard" in (stopped(ids[2])[1] or "")
    clock["t"] += 5
    w._shed_if_memory_high()                      # cooldown: no second victim right away
    assert [stopped(j)[0] for j in ids] == [False, False, True]
    clock["t"] += 130
    w._shed_if_memory_high()                      # still high after the cooldown -> next lane-only job
    assert [stopped(j)[0] for j in ids] == [False, True, True]


def test_memory_dropping_below_the_line_resets_the_clock(shed_worker):
    w, ids, clock, mem, stopped = shed_worker
    mem(95)
    w._shed_if_memory_high()
    clock["t"] += 20
    mem(80)
    w._shed_if_memory_high()                      # recovered: clock cleared
    clock["t"] += 20
    mem(95)
    w._shed_if_memory_high()                      # 40 s since the FIRST sighting but only 0 s since this one
    assert all(not stopped(j)[0] for j in ids)


def test_unknown_memory_never_sheds(shed_worker, monkeypatch):
    w, ids, clock, mem, stopped = shed_worker
    monkeypatch.setattr(server_mod, "_MEM_CACHE", [0.0, None])
    for _ in range(3):
        clock["t"] += 60
        w._shed_if_memory_high()
    assert all(not stopped(j)[0] for j in ids)


def test_a_lone_job_is_never_parked(tmp_path, monkeypatch):
    db_path = tmp_path / "lone.db"
    monkeypatch.setattr(server_mod, "Store", lambda *a, **kw: Store(db_path))
    w = server_mod.Worker()
    seed = Store(db_path)
    jid = seed.create_job(query="q", location=None, max_places=10, delay_sec=0, phase="scraping")
    seed.close()
    with w._lock:
        w._inflight[jid] = None
        w._start_lane[jid] = LANE_WHATSAPP
    clock = {"t": 1000.0}
    monkeypatch.setattr(server_mod.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(server_mod, "_MEM_CACHE", [0.0, {"total_mb": 8365.5, "used_pct": 97}])
    for _ in range(4):
        clock["t"] += 40
        w._shed_if_memory_high()
    s = Store(db_path)
    try:
        assert s.conn.execute("SELECT stop_requested FROM jobs WHERE id=?", (jid,)).fetchone()["stop_requested"] in (0, None)
    finally:
        s.close()


def test_shed_line_is_above_the_start_guard():
    # the start guard (85 %) refuses NEW jobs first; shedding (92 %) only kicks in when that was not enough
    assert MEMORY_SHED_PCT > 85


# ── (3) websites lane: no third Chrome under pressure ─────────────────────────────────────
def test_browser_fallback_gate(monkeypatch):
    from webscraper import enrich, healthcheck as HC
    monkeypatch.setattr(enrich, "_MEM_PCT_CACHE", [0.0, 0.0])
    monkeypatch.setattr(HC, "_memory", lambda: {"used_pct": 85})
    assert enrich.browser_fallback_allowed() is False
    monkeypatch.setattr(enrich, "_MEM_PCT_CACHE", [0.0, 0.0])
    monkeypatch.setattr(HC, "_memory", lambda: {"used_pct": 60})
    assert enrich.browser_fallback_allowed() is True
    monkeypatch.setattr(enrich, "_MEM_PCT_CACHE", [0.0, 0.0])
    monkeypatch.setattr(HC, "_memory", lambda: {"used_pct": None})
    assert enrich.browser_fallback_allowed() is True      # unknown reading never blocks the fallback
    monkeypatch.setattr(enrich, "_MEM_PCT_CACHE", [0.0, 0.0])
    monkeypatch.setattr(HC, "_memory", lambda: (_ for _ in ()).throw(RuntimeError("wmi down")))
    assert enrich.browser_fallback_allowed() is True      # a failing probe never blocks it either


def test_browser_retry_does_not_launch_chrome_when_memory_is_high(monkeypatch, tmp_path):
    """End to end through crawl_site: with RAM at 90 % the blocked site is NOT retried in a browser
    (no BrowserFetcher is ever constructed) and the crawl reports the httpx block; at 60 % it is."""
    import asyncio
    asyncio.run(_browser_retry_scenario(monkeypatch))


async def _browser_retry_scenario(monkeypatch):
    from webscraper import enrich, healthcheck as HC
    import webscraper.browser_fetch as bf
    launched = []
    class _Boom:
        def __init__(self, *a, **kw):
            launched.append(1)
    monkeypatch.setattr(bf, "BrowserFetcher", _Boom)
    monkeypatch.setattr(bf, "BROWSER_FALLBACK", True)
    monkeypatch.setattr(enrich, "_MEM_PCT_CACHE", [0.0, 0.0])
    monkeypatch.setattr(HC, "_memory", lambda: {"used_pct": 90})

    # httpx tier says "blocked" for every page (the exact case that used to launch Chrome)
    async def _blocked(attempt, pool, proxy_first):
        return enrich.Fetched(html=None, error="http_403", url="http://blocked.test/")
    monkeypatch.setattr(enrich, "_with_proxies", _blocked)
    # the same gate enrich_places' closure runs before touching BrowserFetcher
    async def browser_retry(url):
        if not enrich.browser_fallback_allowed():
            return None, None
        launched.append(1)
        return "<html>ok</html>", None
    contacts, reason = await enrich.crawl_site(None, "http://blocked.test/", browser_retry, None, region="VN")  # type: ignore[arg-type]
    assert launched == []                          # no browser was launched
    assert reason is not None                      # the httpx block is what the crawl reports
    # and with memory back to normal the very same crawl does use the browser
    monkeypatch.setattr(enrich, "_MEM_PCT_CACHE", [0.0, 0.0])
    monkeypatch.setattr(HC, "_memory", lambda: {"used_pct": 60})
    contacts, reason = await enrich.crawl_site(None, "http://blocked.test/", browser_retry, None, region="VN")  # type: ignore[arg-type]
    assert launched == [1]


def test_memory_pct_cache_refreshes_every_20s(monkeypatch):
    from webscraper import enrich, healthcheck as HC
    calls = []
    monkeypatch.setattr(HC, "_memory", lambda: calls.append(1) or {"used_pct": 70})
    monkeypatch.setattr(enrich, "_MEM_PCT_CACHE", [0.0, 0.0])
    t = {"v": 1000.0}
    import time as _time
    monkeypatch.setattr(_time, "monotonic", lambda: t["v"])
    enrich.memory_pct_for_browser(); enrich.memory_pct_for_browser()
    assert len(calls) == 1                         # cached
    t["v"] += 21
    enrich.memory_pct_for_browser()
    assert len(calls) == 2                         # refreshed after 20 s
