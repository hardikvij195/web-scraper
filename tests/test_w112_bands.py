"""W112 (CRM T608): Maps discovery runs keyword BANDS across every area and resumes.

Before: all of a job's keywords (UK Health & Medical: 950) ran in ONE area before the next, so an
8 h job searched ~1 of ~89 areas. Now the top TOP_KEYWORDS cover the whole circle first, each
finished (band, area, keyword) is remembered, and a re-run skips what was already searched.
"""
from __future__ import annotations

from webscraper import agent
from webscraper.maps import TOP_KEYWORDS, keyword_bands, plan_steps, step_key
from webscraper.store import Store


def test_keywords_are_split_into_bands():
    qs = [f"k{i}" for i in range(60)]
    bands = keyword_bands(qs)
    assert [len(b) for b in bands] == [TOP_KEYWORDS, TOP_KEYWORDS, 60 - 2 * TOP_KEYWORDS]
    assert bands[0][0] == "k0" and bands[1][0] == f"k{TOP_KEYWORDS}"
    assert keyword_bands([]) == [[""]]


def test_top_band_covers_every_area_before_the_next_band():
    bands = keyword_bands([f"k{i}" for i in range(30)], size=2)
    centers = [(1.0, 1.0), (2.0, 2.0), (3.0, 3.0)]
    steps = plan_steps(bands, centers, 6.25)
    first_band = steps[: 2 * len(centers)]
    assert {s[1] for s in first_band} == set(centers)          # every area…
    assert all(s[3] == 0 for s in first_band)                  # …with the top band only
    assert steps[2 * len(centers)][3] == 1                     # then the next band starts
    assert len(steps) == 30 * len(centers)                     # nothing lost


def test_step_key_is_stable_and_handles_no_centre():
    assert step_key("dentist", (51.5, -0.12), 6.25, 0) == step_key("dentist", (51.500000001, -0.12), 6.25, 0)
    assert step_key("dentist", None, None, 0) == "0|none|0.000|dentist"


def test_collect_steps_roundtrip_and_progress(tmp_path):
    s = Store(tmp_path / "leads.db")
    jid = int(s.create_job(query="q", location="l", max_places=10, delay_sec=0))
    k = step_key("dentist", (51.5, -0.12), 6.25, 0)
    s.mark_collect_step(jid, k)
    s.mark_collect_step(jid, k)                                # idempotent
    assert s.collect_steps_done(jid) == {k}
    s.update_job(jid, collect_stats={"steps_total": 10, "steps_done": 1, "areas_total": 5,
                                     "areas_top_done": 0, "top_keywords": 2, "keywords_total": 2, "pace_sec": 30.0})
    assert agent._local_progress(s.get_job(jid), s)["collect"]["steps_total"] == 10
    s.clear_collect_steps(jid)
    assert s.collect_steps_done(jid) == set()
    s.close()
