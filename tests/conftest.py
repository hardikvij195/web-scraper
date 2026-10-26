"""Shared test defaults. W153 (CRM T1047): the one-job-per-lane rule is ON in production; the suites
written before it (W122 / W135 / W136 / W144 / W149 …) exercise multi-slot gates and round-robin, so
they run with the rule OFF. `tests/test_w153_one_job_per_lane.py` switches it back on itself."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _legacy_lane_rule_off(monkeypatch):
    monkeypatch.setenv("LANE_ONE_JOB_PER_LANE", "0")


@pytest.fixture(autouse=True)
def _fresh_wa_pool(monkeypatch):
    """W200: every test gets its own WhatsApp browser pool; whatever it left open is closed after."""
    from webscraper import wa_verify
    pool = wa_verify.WaPool()
    monkeypatch.setattr(wa_verify, "POOL", pool)
    yield pool
    pool.close_all("test teardown", wait=False)
