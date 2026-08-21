from datetime import datetime, timedelta, timezone

from _agent import claim_is_stale

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def test_fresh_claim_not_stale():
    assert not claim_is_stale((NOW - timedelta(minutes=10)).isoformat(), NOW)


def test_old_claim_is_stale():
    assert claim_is_stale((NOW - timedelta(minutes=31)).isoformat(), NOW)


def test_z_suffix_parses():
    assert claim_is_stale("2026-08-21T11:00:00Z", NOW)
