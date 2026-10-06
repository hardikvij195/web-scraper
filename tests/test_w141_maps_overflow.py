"""W141 (CRM T1019) -> W144: the Maps tab never idles behind jobs sitting in enrichment / WhatsApp.

W141 gave a discovery job ONE overflow slot above the job cap. W144 removed the job cap: a discovery
job starts whenever the Maps tab is free, however many jobs are draining their other lanes, bounded
only by the crash guard (`max_inflight_jobs()`). The intent these tests keep: Maps never idles, a busy
Maps tab blocks any discovery job, and never two discovery jobs at once."""
from webscraper.server import LANE_DISCOVERY, LANE_ENRICHMENT, may_start_job

GUARD = 8


def _args(**kw):
    base = dict(disc_free=True, enrich_free=0, wa_free=0, wa_account=False, n_inflight=0, max_inflight=GUARD)
    base.update(kw)
    return base


def test_discovery_job_starts_whenever_the_maps_tab_is_free_even_with_many_jobs_in_flight():
    for n in range(GUARD):
        assert may_start_job(LANE_DISCOVERY, **_args(n_inflight=n)) is True, n


def test_full_enrichment_and_whatsapp_lanes_do_not_hold_a_discovery_job():
    assert may_start_job(LANE_DISCOVERY, **_args(enrich_free=0, wa_free=0, n_inflight=5)) is True
    assert may_start_job(LANE_ENRICHMENT, **_args(enrich_free=0, n_inflight=5)) is False   # its own lane is full


def test_busy_maps_tab_blocks_a_discovery_job():
    assert may_start_job(LANE_DISCOVERY, **_args(disc_free=False, n_inflight=1)) is False
    assert may_start_job(LANE_DISCOVERY, **_args(disc_free=False, n_inflight=0)) is False


def test_crash_guard_is_the_only_job_count_that_matters():
    # W149: ONE overflow slot above the guard for a discovery job (ASUS 8/8 WhatsApp-only, Maps idle 8 h)
    assert may_start_job(LANE_DISCOVERY, **_args(n_inflight=GUARD)) is True
    assert may_start_job(LANE_DISCOVERY, **_args(n_inflight=GUARD + 1)) is False
    assert may_start_job(LANE_DISCOVERY, **_args(n_inflight=GUARD - 1)) is True
    assert may_start_job(LANE_ENRICHMENT, **_args(enrich_free=1, n_inflight=GUARD)) is False   # lane-only: no overflow


def test_memory_guard_still_holds_the_overflow_slot():
    assert may_start_job(LANE_DISCOVERY, **_args(n_inflight=GUARD, mem_blocked=True)) is False


def test_tail_keep_lets_an_almost_finished_lane_keep_its_slot():
    from webscraper.lanes import tail_keep
    assert tail_keep("whatsapp", 25) is True
    assert tail_keep("whatsapp", 50) is True
    assert tail_keep("whatsapp", 51) is False
    assert tail_keep("whatsapp", 0) is False
    assert tail_keep("whatsapp", None) is False
    assert tail_keep("enrichment", 40) is True
    assert tail_keep("discovery", 5) is False
