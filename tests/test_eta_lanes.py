"""Pure-logic tests for the concurrent-lane ETA model (webscraper/eta.py).

No network, no Playwright, and deliberately **no SQLite** — every fixture is a plain dict
shaped like a `jobs` row, and the rate history is a two-line fake Store. `eta` reads its row
through `_get`, which accepts dicts and sqlite3.Row alike, so a dict exercises exactly the
code path production uses without touching `data/leads.db`.

What is pinned here is the behaviour the sequential model got wrong:

* two lanes can be 'running' at the same time (state comes from each lane's own stamps),
* the job ETA is max(), not sum(), because the lanes overlap in wall-clock,
* a lane that ended with ok=0 is 'failed' and still carries its reason,
* a downstream total is a floor (`total_is_min`) while discovery keeps feeding it,
* an unestimable lane makes the whole job `estimating`, never a made-up number.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from webscraper import eta


NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)


def iso(seconds_ago: float) -> str:
    """An ISO stamp `seconds_ago` before the frozen NOW (negative = in the future)."""
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


class FakeStore:
    """Just the one method `eta` calls. Seconds-per-unit per phase, or None for 'no history'."""

    def __init__(self, **rates: float | None) -> None:
        self.rates = rates

    def phase_rate(self, phase: str) -> float | None:
        return self.rates.get(phase)


def job(**over) -> dict:
    """A jobs row mid-flight, with every column eta reads present. Override per test."""
    base = {
        "id": 1,
        "phase": "scraping",            # stays 'scraping' for the whole lane run (see server.py)
        "status": "running",
        "max_places": 0,
        "max_minutes": None,
        "maps_deadline_at": None,
        "enrich_deadline_at": None,
        "finished_at": None,
        "do_enrich": 1,
        "do_research": 0,
        "do_wa_verify": 1,
        "wa_verify_only": 0,
        "reenrich_only": 0,
        "scraped_count": 0, "links_found": 0,
        "enrich_done": 0, "enrich_total": 0,
        "research_done": 0, "research_total": 0,
        "wa_verify_done": 0, "wa_verify_total": 0,
        "scrape_started_at": None, "disc_ended_at": None, "disc_ok": None, "disc_reason": None,
        "enrich_started_at": None, "enr_ended_at": None, "enr_ok": None, "enr_reason": None,
        "wa_started_at": None, "wa_ended_at": None, "wa_ok": None, "wa_reason": None,
    }
    base.update(over)
    return base


def by_key(rows: list[dict]) -> dict[str, dict]:
    return {r["key"]: r for r in rows}


# -- concurrency: more than one lane is 'running' -----------------------------------
def test_two_lanes_running_at_once() -> None:
    """Discovery and enrichment both started, neither ended -> BOTH 'running'.

    The old model derived state from a position in PHASE_ORDER, so exactly one entry could
    ever be 'running' and enrichment showed as 'pending' while it was demonstrably working.
    """
    row = job(scrape_started_at=iso(120), scraped_count=30, links_found=60,
              enrich_started_at=iso(60), enrich_done=10, enrich_total=30)
    ln = by_key(eta.lanes(row, FakeStore(), NOW))
    assert ln["discovery"]["status"] == "running"
    assert ln["enrichment"]["status"] == "running"
    # WhatsApp is enabled for this job but has not started -> pending means "not started",
    # not "waiting its turn behind the others".
    assert ln["whatsapp"]["status"] == "pending"


def test_pending_means_not_started_even_when_a_later_lane_is_done() -> None:
    """Lane order is not causality: WhatsApp can be finished while enrichment still runs."""
    row = job(scrape_started_at=iso(300), disc_ended_at=iso(60), disc_ok=1, disc_reason="completed",
              enrich_started_at=iso(280), enrich_done=20, enrich_total=40,
              wa_started_at=iso(290), wa_ended_at=iso(30), wa_ok=1, wa_reason="no_targets")
    ln = by_key(eta.lanes(row, FakeStore(), NOW))
    assert ln["discovery"]["status"] == "done"
    assert ln["enrichment"]["status"] == "running"
    assert ln["whatsapp"]["status"] == "done"


def test_legacy_phases_view_follows_its_lane() -> None:
    """The four-entry CRM shape survives, and 'researching' inherits the enrichment lane."""
    row = job(do_research=1, scrape_started_at=iso(120), scraped_count=30, links_found=60,
              enrich_started_at=iso(60), enrich_done=10, enrich_total=30)
    ph = by_key(eta.phases(row, FakeStore(), NOW))
    assert list(ph) == [p for p in eta.PHASE_ORDER if p in ph]      # order preserved
    assert ph["scraping"]["status"] == "running"
    assert ph["enriching"]["status"] == "running"
    assert ph["researching"]["status"] == "running"                 # folded into enrichment
    assert ph["researching"]["lane"] == "enrichment"
    assert ph["verifying_wa"]["status"] == "pending"


# -- the job ETA is max(), not sum() ------------------------------------------------
def test_job_eta_is_max_of_lanes_not_sum() -> None:
    """Concurrent lanes finish when the SLOWEST one does.

    discovery  10 of 20 places  @ 4 s ->  40 s
    enrichment  0 of 20 leads   @ 6 s -> 120 s   <- the slow one
    whatsapp    0 of 20 numbers @ 3 s ->  60 s
    max = 120; the old sum would have said 220.
    """
    row = job(scrape_started_at=iso(40), scraped_count=10, links_found=20,
              enrich_started_at=iso(40), enrich_done=0, enrich_total=20,
              wa_started_at=iso(40), wa_verify_done=0, wa_verify_total=20)
    store = FakeStore(scraping=4.0, enriching=6.0, verifying_wa=3.0)
    s = eta.summarise(row, store, NOW)
    ln = by_key(s["lanes"])
    assert ln["discovery"]["eta_sec"] == 40
    assert ln["enrichment"]["eta_sec"] == 120
    assert ln["whatsapp"]["eta_sec"] == 60
    assert s["eta_sec"] == 120                      # max, NOT 220
    assert s["estimating"] is False


def test_finished_job_has_zero_eta() -> None:
    row = job(phase="done", status="done", finished_at=iso(0),
              scrape_started_at=iso(600), disc_ended_at=iso(120), disc_ok=1, disc_reason="completed",
              enrich_started_at=iso(590), enr_ended_at=iso(60), enr_ok=1, enr_reason="completed",
              wa_started_at=iso(580), wa_ended_at=iso(10), wa_ok=1, wa_reason="completed")
    s = eta.summarise(row, FakeStore(scraping=4.0), NOW)
    assert s["eta_sec"] == 0
    assert s["estimating"] is False
    assert all(lane["status"] == "done" for lane in s["lanes"])


# -- a lane that gave up ------------------------------------------------------------
def test_lane_ended_not_ok_is_failed_and_keeps_its_reason() -> None:
    """ok=0 -> 'failed', with the token that says what actually happened.

    This is the "2 / 30 numbers - done" bug: the WhatsApp lane hit its daily cap and the UI
    reported success. The reason must survive to the UI, and `ok` must read False.
    """
    row = job(scrape_started_at=iso(600), disc_ended_at=iso(300), disc_ok=1, disc_reason="maps_cap",
              enrich_started_at=iso(590), enrich_done=30, enrich_total=30,
              wa_started_at=iso(580), wa_verify_done=2, wa_verify_total=30,
              wa_ended_at=iso(120), wa_ok=0, wa_reason="wa_daily_cap")
    ln = by_key(eta.lanes(row, FakeStore(), NOW))
    wa = ln["whatsapp"]
    # A cap is a planned stop, not a failure (2026-08-25): 'stopped', never 'done'.
    assert wa["status"] == "stopped"
    assert wa["ok"] is False
    assert wa["reason"] == "wa_daily_cap"
    # Only an error: reason reads 'failed'.
    row2 = job(scrape_started_at=iso(600), disc_ended_at=iso(300), disc_ok=0, disc_reason="error:boom")
    assert by_key(eta.lanes(row2, FakeStore(), NOW))["discovery"]["status"] == "failed"
    assert wa["eta_sec"] is None and wa["estimating"] is False   # over; nothing to wait for
    assert wa["ran_sec"] == 460                                  # 580s ago -> 120s ago
    # A lane can end 'ok' and still owe the user an explanation — maps_cap means the search
    # was cut short, which is why the reason travels regardless of ok.
    assert ln["discovery"]["reason"] == "maps_cap"


def test_error_reason_is_passed_through() -> None:
    row = job(enrich_started_at=iso(90), enr_ended_at=iso(30), enr_ok=0,
              enr_reason="error:httpx.ConnectTimeout")
    ln = by_key(eta.lanes(row, FakeStore(), NOW))
    assert ln["enrichment"]["status"] == "failed"
    assert ln["enrichment"]["reason"] == "error:httpx.ConnectTimeout"


def test_ran_sec_is_live_while_running_and_settled_once_ended() -> None:
    row = job(scrape_started_at=iso(75),
              enrich_started_at=iso(200), enr_ended_at=iso(50), enr_ok=1, enr_reason="completed")
    ln = by_key(eta.lanes(row, FakeStore(), NOW))
    assert ln["discovery"]["ran_sec"] == 75      # still going: now - started
    assert ln["enrichment"]["ran_sec"] == 150    # ended - started
    assert ln["whatsapp"]["ran_sec"] is None     # never started


# -- totals are a floor while discovery keeps feeding -------------------------------
def test_total_is_min_while_discovery_runs() -> None:
    """More leads are still arriving, so 20 is a lower bound, not a forecast."""
    row = job(scrape_started_at=iso(60), scraped_count=20, links_found=20,
              enrich_started_at=iso(50), enrich_done=5, enrich_total=20)
    ln = by_key(eta.lanes(row, FakeStore(enriching=2.0), NOW))
    assert ln["discovery"]["total_is_min"] is False      # discovery's own total is not a floor
    assert ln["enrichment"]["total_is_min"] is True
    assert ln["whatsapp"]["total_is_min"] is True
    # ...and the same flag reaches the legacy phase entries the CRM already renders.
    ph = by_key(eta.phases(row, FakeStore(enriching=2.0), NOW))
    assert ph["enriching"]["total_is_min"] is True
    assert ph["scraping"]["total_is_min"] is False


def test_total_is_final_once_discovery_has_ended() -> None:
    row = job(scrape_started_at=iso(300), disc_ended_at=iso(60), disc_ok=1, disc_reason="completed",
              scraped_count=20, links_found=20,
              enrich_started_at=iso(290), enrich_done=5, enrich_total=20)
    ln = by_key(eta.lanes(row, FakeStore(enriching=2.0), NOW))
    assert ln["enrichment"]["total_is_min"] is False
    assert ln["whatsapp"]["total_is_min"] is False


# -- never invent a number ----------------------------------------------------------
def test_unestimable_lane_makes_the_whole_job_estimating() -> None:
    """No WhatsApp history and too few live samples -> that lane, and the job, say so."""
    row = job(scrape_started_at=iso(60), scraped_count=20, links_found=20,
              enrich_started_at=iso(60), enrich_done=10, enrich_total=20,
              wa_started_at=iso(5), wa_verify_done=1, wa_verify_total=20)
    store = FakeStore(scraping=4.0, enriching=2.0, verifying_wa=None)
    s = eta.summarise(row, store, NOW)
    ln = by_key(s["lanes"])
    assert ln["whatsapp"]["estimating"] is True
    assert ln["whatsapp"]["eta_sec"] is None
    assert ln["whatsapp"]["rate_source"] == "none"
    assert s["eta_sec"] is None          # a max over a partial set would under-report
    assert s["estimating"] is True


def test_live_rate_needs_both_enough_samples_and_enough_elapsed() -> None:
    """A just-restarted lane with inherited counters must not quote a near-zero pace."""
    fresh = job(scrape_started_at=iso(2), scraped_count=50, links_found=100)
    ln = by_key(eta.lanes(fresh, FakeStore(scraping=4.0), NOW))
    # 2s elapsed is below MIN_LIVE_ELAPSED_SEC -> fall back to the banked 4 s/place.
    assert ln["discovery"]["rate_source"] == "history"
    assert ln["discovery"]["eta_sec"] == 200

    settled = job(scrape_started_at=iso(200), scraped_count=50, links_found=100)
    ln2 = by_key(eta.lanes(settled, FakeStore(scraping=4.0), NOW))
    assert ln2["discovery"]["rate_source"] == "live"      # 200s / 50 = 4 s exactly
    assert ln2["discovery"]["eta_sec"] == 200


def test_unlimited_discovery_falls_back_to_the_maps_cap() -> None:
    """max_places=0 and nothing collected yet: the only honest answer is 'until the cap'."""
    row = job(max_places=0, links_found=0, scraped_count=0,
              scrape_started_at=iso(30), maps_deadline_at=iso(-600))   # 600s in the future
    ln = by_key(eta.lanes(row, FakeStore(), NOW))
    assert ln["discovery"]["rate_source"] == "budget"
    assert ln["discovery"]["eta_sec"] == 600


def test_discovery_eta_never_outruns_the_maps_cap() -> None:
    """3600s of work left but only 120s of Maps budget: report the cap, not the work."""
    row = job(scrape_started_at=iso(400), scraped_count=100, links_found=1000,
              maps_deadline_at=iso(-120))
    ln = by_key(eta.lanes(row, FakeStore(scraping=4.0), NOW))
    assert ln["discovery"]["eta_sec"] == 120


def test_downstream_lanes_are_not_capped_by_the_enrich_deadline() -> None:
    """`enrich_deadline_at` binds nothing now — enrichment drains the backlog regardless.

    Capping here would quote a finish time the worker has no intention of honouring.
    """
    row = job(scrape_started_at=iso(300), disc_ended_at=iso(60), disc_ok=1, disc_reason="completed",
              enrich_started_at=iso(60), enrich_done=0, enrich_total=100,
              enrich_deadline_at=iso(30))          # already expired
    ln = by_key(eta.lanes(row, FakeStore(enriching=5.0), NOW))
    assert ln["enrichment"]["eta_sec"] == 500      # full remaining work, not 0


# -- which lanes a job runs at all --------------------------------------------------
def test_disabled_lanes_are_reported_and_ignored_by_the_job_eta() -> None:
    """A wa_verify_only re-run has no discovery/enrichment to show, and they must not
    drag the job ETA into 'estimating' by looking unestimable."""
    row = job(wa_verify_only=1, do_enrich=1, do_wa_verify=0,
              wa_started_at=iso(200), wa_verify_done=50, wa_verify_total=100)
    s = eta.summarise(row, FakeStore(verifying_wa=2.0), NOW)
    ln = by_key(s["lanes"])
    assert [lane["key"] for lane in s["lanes"]] == eta.LANE_ORDER      # always three entries
    assert ln["discovery"]["status"] == "disabled"
    assert ln["enrichment"]["status"] == "disabled"
    assert ln["discovery"]["reason"] == "disabled"
    assert ln["whatsapp"]["status"] == "running"
    # 200s for 50 numbers is enough live evidence, so the observed 4 s/number wins over the
    # banked 2 s — 50 left * 4 s. The point of the test is that the job ETA equals the ONE
    # live lane's ETA and the two disabled lanes contribute nothing.
    assert ln["whatsapp"]["rate_source"] == "live"
    assert s["eta_sec"] == 200 == ln["whatsapp"]["eta_sec"]
    # ...and the legacy view drops them entirely, as it always did.
    assert [p["key"] for p in s["phases"]] == ["verifying_wa"]


def test_enrichment_off_disables_only_that_lane() -> None:
    row = job(do_enrich=0, scrape_started_at=iso(30))
    ln = by_key(eta.lanes(row, FakeStore(), NOW))
    assert ln["enrichment"]["status"] == "disabled"
    assert ln["discovery"]["status"] == "running"
    assert ln["whatsapp"]["status"] == "pending"


# -- backwards compatibility --------------------------------------------------------
def test_row_without_lane_columns_does_not_crash() -> None:
    """A job row created before the lane columns existed: only the pre-lane keys present.

    `do_wa_verify` is absent too, so the WhatsApp lane reads 'disabled' rather than
    inventing work the job never asked for.
    """
    old = {"id": 7, "phase": "enriching", "status": "running",
           "scraped_count": 12, "links_found": 12, "enrich_done": 3, "enrich_total": 12,
           "scrape_started_at": iso(300), "enrich_started_at": iso(100)}
    s = eta.summarise(old, FakeStore(scraping=4.0, enriching=5.0), NOW)
    ln = by_key(s["lanes"])
    assert ln["discovery"]["status"] == "running"     # started, never ended
    assert ln["enrichment"]["status"] == "running"
    assert ln["whatsapp"]["status"] == "disabled"
    assert ln["whatsapp"]["reason"] == "disabled"
    assert isinstance(s["phases"], list) and s["phases"]


def test_old_terminal_row_with_no_lane_end_is_not_still_running() -> None:
    """Process killed mid-run (or a pre-columns row): the job is over, so the lanes are."""
    row = job(phase="stopped", status="stopped", finished_at=iso(10),
              scrape_started_at=iso(310), enrich_started_at=iso(300))
    ln = by_key(eta.lanes(row, FakeStore(scraping=4.0), NOW))
    assert ln["discovery"]["status"] == "stopped"
    assert ln["enrichment"]["status"] == "stopped"
    assert ln["discovery"]["ran_sec"] == 300          # start -> finished_at
    assert eta.summarise(row, FakeStore(), NOW)["eta_sec"] == 0


def test_summarise_keeps_the_legacy_keys() -> None:
    """The CRM and server._job_dict read these by name — none may disappear."""
    row = job(scrape_started_at=iso(200), scraped_count=50, links_found=100,
              maps_deadline_at=iso(-300))
    s = eta.summarise(row, FakeStore(scraping=4.0), NOW)
    for key in ("phases", "lanes", "eta_sec", "phase", "phase_eta_sec", "estimating",
                "budget_left_sec"):
        assert key in s, key
    assert s["phase"] == "scraping"
    assert s["budget_left_sec"] == 300               # Maps cap, reported while discovery runs


def test_budget_left_is_none_once_discovery_is_over() -> None:
    """Nothing else obeys a deadline, so there is no budget left to report."""
    row = job(scrape_started_at=iso(400), disc_ended_at=iso(60), disc_ok=1, disc_reason="maps_cap",
              maps_deadline_at=iso(-300), enrich_started_at=iso(390), enrich_done=1, enrich_total=10)
    s = eta.summarise(row, FakeStore(enriching=3.0), NOW)
    assert s["budget_left_sec"] is None
