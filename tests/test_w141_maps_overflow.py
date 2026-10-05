"""W141 (CRM T1019, owner: "a google maps lane for any job running 24x7"): one overflow slot above
max_inflight is reserved for a job that needs Maps, so the Maps tab never idles behind jobs that
hold every slot in enrichment / WhatsApp."""
from __future__ import annotations

from webscraper.server import may_start_job

MAX = 3


def test_discovery_job_takes_the_overflow_slot_at_max():
    assert may_start_job(True, MAX, MAX, disc_free=True) is True


def test_non_discovery_job_still_obeys_max_inflight():
    assert may_start_job(False, MAX, MAX, disc_free=True) is False
    assert may_start_job(False, MAX - 1, MAX, disc_free=False) is True      # W138 unchanged


def test_second_discovery_job_does_not_start_while_the_overflow_runs():
    # overflow job running: 4 in flight, Maps busy -> no second one, whichever way it is judged
    assert may_start_job(True, MAX + 1, MAX, disc_free=False) is False
    # its discovery done (slot free again) but still 4 in flight -> still no second overflow
    assert may_start_job(True, MAX + 1, MAX, disc_free=True) is False


def test_busy_maps_tab_blocks_a_discovery_job_below_max_too():
    assert may_start_job(True, 1, MAX, disc_free=False) is False
