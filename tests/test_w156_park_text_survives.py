"""W156 (CRM T1047): the park text (W151 / W153) must survive the job-end summary write so the CRM's
`lead_gen_requeue_orphans()` can match it. Checks the composed note shape used in `Worker._run_job`."""
from __future__ import annotations

from webscraper import lanes as L


def test_park_prefix_is_kept_in_front_of_the_summary():
    prev = L.PARK_MSG.format(lane="whatsapp", holder=22562)
    summary = "discovery: completed · enrichment: completed · whatsapp: stopped"
    note = f"{prev} · {summary}" if prev.startswith("parked by") else summary
    assert note.startswith("parked by the lane rule (W153): the whatsapp lane on this machine is busy with job #22562")
    assert note.endswith(summary)
    assert "parked by the lane rule" in note[:300], "the CRM stores the first 300 chars"
