"""Lane model + estimated time remaining for a job (T136 / W2 + W4).

**A job is no longer a chain.** Since 2026-08-23 (`webscraper/lanes.py`) one job runs three
lanes *at the same time*, each in its own thread, using the `places` table as the queue
between them:

    discovery    Google Maps            <- the ONLY lane `max_minutes` caps
    enrichment   site + socials (httpx), then the AI summary for that same lead
    whatsapp     WhatsApp Web verification

So "which phase is the job in" is the wrong question: two or three of them are live at once,
and a lane is `pending` because it has not *started* -- not because some earlier lane has not
finished. Every state here is therefore derived from that lane's **own** start/end stamps
(`scrape_started_at` / `disc_ended_at` / `disc_ok` / `disc_reason` and friends, written by
`Store.lane_start` / `lane_end`), never from a position in a list.

Two consequences worth spelling out, because both were bugs in the sequential model:

* **The whole-job ETA is `max()` of the lane ETAs, not `sum()`.** Adding concurrent work up
  double-counts wall-clock and told the user 20 minutes for a 9-minute job.
* **Enrichment/WhatsApp totals are a floor while discovery still runs** -- more leads keep
  landing in the queue. Those entries carry `total_is_min: True` so the UI can render
  "at least 40" instead of pretending 40 is the final figure.

`enrich_deadline_at` is deliberately NOT used as a cap any more: enrichment and WhatsApp
drain the backlog however long it takes, so capping their ETA at that stamp would report a
finish time the worker has no intention of honouring.

Compatibility is load-bearing. `phases` keeps its exact four-entry shape
(scraping / enriching / researching / verifying_wa) because the CRM and the local UI already
render it -- the new `lanes` block sits *beside* it, and a consumer that ignores `lanes`
still works. A job row written before the lane columns existed must not crash either: every
read goes through `_get`, and a finished job whose lanes never recorded an end falls back to
the job's own status.

**No hardcoded rate constants.** A lane's seconds-per-unit comes from (a) what this very run
has already done, once there are enough samples for it to mean anything, else (b) a rolling
average over the last N runs of that phase on this machine, else (c) nothing -- and
"nothing" is reported as `estimating: true`, never as a made-up number.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from webscraper.config import settings

# The legacy four-entry view. Kept verbatim: the CRM's progress strip is keyed on these
# names, and an agent shipping the old payload must keep working (see module docstring).
PHASE_ORDER = ["scraping", "enriching", "researching", "verifying_wa"]

LABELS = {
    "scraping": "Google Maps discovery",
    "enriching": "Website, socials & WhatsApp",
    "researching": "AI research & summary",
    "verifying_wa": "WhatsApp verification",
}

UNITS = {
    "scraping": "places",
    "enriching": "sites",
    "researching": "businesses",
    "verifying_wa": "numbers",
}

# -- the real execution model -------------------------------------------------------
#: The three threads `lanes.Pipeline` actually starts, in the order the UI lists them.
LANE_ORDER = ["discovery", "enrichment", "whatsapp"]

LANE_LABELS = {
    "discovery": "Google Maps discovery",
    "enrichment": "Websites, socials & AI research",
    "whatsapp": "WhatsApp verification",
}

LANE_UNITS = {
    "discovery": "places",
    "enrichment": "businesses",
    "whatsapp": "numbers",
}

#: Which lane thread actually performs each legacy phase. AI research has no lane of its
#: own -- `lanes.EnrichmentLane` folds it in per batch -- so 'researching' maps to
#: enrichment and inherits its state. That is the honest answer: when enrichment is
#: running, research is running.
PHASE_LANE = {
    "scraping": "discovery",
    "enriching": "enrichment",
    "researching": "enrichment",
    "verifying_wa": "whatsapp",
}

#: Which banked `phase_rates` series prices one unit of a lane. The enrichment lane is
#: priced off 'enriching' alone even when research is on: the live rate (elapsed since
#: enrich_started_at over enrich_done) already includes the research time spent on the same
#: leads, and the historical 'researching' series covers only the subset that had a website,
#: so adding it in would systematically over-quote.
LANE_RATE_PHASE = {"discovery": "scraping", "enrichment": "enriching", "whatsapp": "verifying_wa"}

#: Mirror of `Store.LANE_COLS` -- (started, ended, ok, reason). Duplicated on purpose so
#: this module stays pure/importable without a DB handle; keep the two in step.
LANE_COLS = {
    "discovery": ("scrape_started_at", "disc_ended_at", "disc_ok", "disc_reason"),
    "enrichment": ("enrich_started_at", "enr_ended_at", "enr_ok", "enr_reason"),
    "whatsapp": ("wa_started_at", "wa_ended_at", "wa_ok", "wa_reason"),
}

#: (done column, total column) per lane. Enrichment counts leads, not sites, because that
#: is what its queue holds.
LANE_COUNTS = {
    "discovery": ("scraped_count", "links_found"),
    "enrichment": ("enrich_done", "enrich_total"),
    "whatsapp": ("wa_verify_done", "wa_verify_total"),
}

#: Lanes fed by discovery: their totals grow while discovery is still writing rows.
DOWNSTREAM = ("enrichment", "whatsapp")

# Phases that run inside a lane other than discovery -- they size themselves from what Maps
# has found when they have no total of their own yet.
POST_MAPS = ("enriching", "researching", "verifying_wa")

# How many units the current run must have finished before its own observed pace is
# trusted over the historical average. Scraping is metronomic (fixed delay per place) so
# 3 is plenty; the enricher is concurrent and site-dependent, so it needs a longer look.
MIN_LIVE_SAMPLES = {"scraping": 3, "enriching": 5, "researching": 3, "verifying_wa": 3}

# ...and the phase must also have been running for this long. Counters can be non-zero
# the instant a phase's start stamp is (re)written -- a resumed job, or a phase that
# inherits work already done -- which would compute a near-zero seconds-per-unit and
# promise a finish that is seconds away. Below this, fall back to the history.
MIN_LIVE_ELAPSED_SEC = 20.0

TERMINAL = ("done", "stopped", "failed", "cancelled", "error")

#: Lane statuses that still have work ahead of them (and therefore an ETA that matters).
UNFINISHED = ("running", "pending")

#: Reason token -> prose. Unknown tokens (notably `error:<detail>`) are passed through by
#: the callers; this map exists so the local UI and the CRM word them identically.
REASON_TEXT = {
    "completed": "finished",
    "no_targets": "nothing to do",
    "maps_cap": "hit the time limit",
    "stopped": "stopped by you",
    "wa_daily_cap": "daily WhatsApp cap reached",
    "wa_not_logged_in": "WhatsApp Web not logged in",
    "disabled": "not requested",
}



def _at_least(total: int | None, done: int) -> int | None:
    """A lane's total can lag its done count after a resume; never show 77 / 29."""
    return None if total is None else max(total, done)

def _get(row: Any, key: str, default: Any = None) -> Any:
    """Read a column from an sqlite3.Row or a plain dict without blowing up on absence.

    Absence is the normal case for a job row created before the lane columns landed, so
    this must stay total -- never let an old row raise out of `phases()` / `lanes()`.
    """
    try:
        v = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if v is None else v


def _ts(s: Any) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    # Rows written before the tz-aware now_iso() (and any hand-edited value) are naive;
    # treat them as UTC so the subtraction below never raises.
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _left(deadline: Any, now: datetime) -> Optional[float]:
    d = _ts(deadline)
    return max(0.0, (d - now).total_seconds()) if d else None


def budgets(max_minutes: Optional[int]) -> dict[str, Optional[float]]:
    """Seconds each half of a job gets, from `max_minutes` and the configured split.

    Returned even when max_minutes is None (all None = no cap), so callers have one
    shape to deal with. NOTE: since the lanes rewrite, `maps_sec` is the only figure that
    actually governs anything at runtime -- `enrich_sec` is still written to
    `enrich_deadline_at` for the record, but no lane obeys it.
    """
    if not max_minutes:
        return {"maps_sec": None, "enrich_sec": None, "collect_sec": None}
    total = float(max_minutes) * 60.0
    maps_sec = total * max(0.0, settings.maps_budget_frac)
    enrich_sec = total * max(0.0, settings.enrich_budget_frac)
    return {
        "maps_sec": maps_sec,
        # 0 (or a negative config) means "do not cap the post-Maps phases at all".
        "enrich_sec": enrich_sec or None,
        "collect_sec": maps_sec * min(max(settings.collect_budget_frac, 0.0), 1.0),
    }


def _rate(store: Any, phase: str, done: int, started: Optional[datetime],
          now: datetime) -> tuple[Optional[float], str]:
    """Seconds per unit for `phase`, plus where the figure came from ('live'|'history')."""
    need = MIN_LIVE_SAMPLES.get(phase, 3)
    if started and done >= need:
        elapsed = (now - started).total_seconds()
        if elapsed >= MIN_LIVE_ELAPSED_SEC:
            return elapsed / done, "live"
    hist = store.phase_rate(phase) if store is not None else None
    return (hist, "history") if hist else (None, "none")


def _expected_leads(row: Any) -> Optional[int]:
    """How many businesses the downstream lanes will have to chew through.

    Before enrichment starts nothing has counted them yet, so fall back to what Maps
    found (or is going to find). An unlimited job (max_places = 0) before any link has
    been collected is genuinely unknowable -- return None and let the caller say so.
    """
    for key in ("enrich_total", "links_found", "scraped_count", "max_places"):
        v = int(_get(row, key, 0) or 0)
        if v > 0:
            return v
    return None


def _job_status(row: Any) -> str:
    return str(_get(row, "phase", "") or _get(row, "status", "") or "")


def lane_enabled(row: Any) -> dict[str, bool]:
    """Which lanes this job will actually run -- mirrors `lanes.*Lane.enabled()`.

    A `wa_verify_only` re-run touches nothing but WhatsApp, and a `reenrich_only` retry
    skips Maps; showing greyed-out bars for lanes that will never start is a lie about
    what the job is doing, so the UI is told they are 'disabled'.
    """
    wa_only = bool(_get(row, "wa_verify_only", 0))
    reenrich_only = bool(_get(row, "reenrich_only", 0))
    return {
        "discovery": not wa_only and not reenrich_only,
        "enrichment": bool(_get(row, "do_enrich", 1)) and not wa_only,
        "whatsapp": bool(_get(row, "do_wa_verify", 0)) or wa_only,
    }


def phase_enabled(row: Any) -> dict[str, bool]:
    """Which of the four legacy phases this job runs (the shape the CRM already renders)."""
    wa_only = bool(_get(row, "wa_verify_only", 0))
    return {
        "scraping": not wa_only and not bool(_get(row, "reenrich_only", 0)),
        "enriching": bool(_get(row, "do_enrich", 1)) and not wa_only,
        "researching": bool(_get(row, "do_research", 0)) and not wa_only,
        "verifying_wa": bool(_get(row, "do_wa_verify", 0)) or wa_only,
    }


def _lane_state(row: Any, lane: str, now: datetime) -> dict[str, Any]:
    """State of ONE lane, from that lane's own stamps -- never from a position in a list.

        ended present     -> done (ok truthy) / failed (ok == 0)
        started, no ended -> running   <- two lanes can both be here at the same time
        neither           -> pending   <- "has not started", not "waiting its turn"

    The job-terminal fallback covers two real cases: a row written before the lane columns
    existed, and a lane whose `lane_end` never ran because the process was killed. Either
    way the job is over, so the lane cannot still be 'running'.
    """
    started_c, ended_c, ok_c, reason_c = LANE_COLS[lane]
    started = _ts(_get(row, started_c))
    ended = _ts(_get(row, ended_c))
    ok_raw = _get(row, ok_c)
    reason = _get(row, reason_c)
    status = _job_status(row)

    if ended is not None:
        # ok is NULL only on a pre-columns row that somehow has an end stamp; treat the
        # presence of an end as success rather than inventing a failure.
        ok = True if ok_raw is None else bool(ok_raw)
        state = "done" if ok else "failed"
    elif status in TERMINAL:
        state = "done" if status == "done" else status
        ok = None if ok_raw is None else bool(ok_raw)
        ended = _ts(_get(row, "finished_at"))
    elif started is not None:
        state, ok = "running", None
    else:
        state, ok = "pending", None

    # ran_sec = how long this lane has been going: settled once it ends, live while it runs.
    if started and (ended or state == "running"):
        ran: Optional[float] = max(0.0, ((ended or now) - started).total_seconds())
    else:
        ran = None
    return {"status": state, "started": started, "ended": ended, "ok": ok,
            "reason": reason, "ran_sec": None if ran is None else round(ran)}


def lanes(row: Any, store: Any = None, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """The three concurrent lanes, each with its own state, runtime, reason and ETA.

    Always three entries, in `LANE_ORDER`, so consumers can rely on the shape; a lane this
    job will not run has `status: 'disabled'` and is excluded from the job ETA.
    """
    now = now or datetime.now(timezone.utc)
    en = lane_enabled(row)
    maps_left = _left(_get(row, "maps_deadline_at"), now)
    states = {k: _lane_state(row, k, now) for k in LANE_ORDER}
    # While discovery can still write new rows, everything downstream of it is sized off a
    # moving target -- its `total` is a floor, not a forecast.
    disc_open = en["discovery"] and states["discovery"]["status"] in UNFINISHED

    out: list[dict[str, Any]] = []
    for key in LANE_ORDER:
        st = states[key]
        done_c, total_c = LANE_COUNTS[key]
        done = int(_get(row, done_c, 0) or 0)
        total: Optional[int] = int(_get(row, total_c, 0) or 0) or None
        total_is_min = False
        if key in DOWNSTREAM:
            if total is None:
                total = _expected_leads(row)
            if total is not None and disc_open:
                total_is_min = True

        status = "disabled" if not en[key] else st["status"]
        eta: Optional[float] = None
        src = "done"
        if status in UNFINISHED:
            per, src = _rate(store, LANE_RATE_PHASE[key], done,
                             st["started"] if status == "running" else None, now)
            if per is not None and total is not None:
                eta = max(0.0, total - done) * per
            elif key == "discovery" and total is None and maps_left is not None:
                # Unlimited discovery: the only honest estimate is "until the cap".
                eta, src = maps_left, "budget"
            # Discovery is the one lane with a hard stop; the other two drain the backlog
            # for as long as it takes, so nothing caps them.
            if key == "discovery" and eta is not None and maps_left is not None:
                eta = min(eta, maps_left)

        out.append({
            "key": key,
            "label": LANE_LABELS[key],
            "unit": LANE_UNITS[key],
            "status": status,
            "done": done,
            "total": total,
            # True = `total` is a lower bound (discovery is still feeding this lane).
            "total_is_min": total_is_min,
            "eta_sec": round(eta) if eta is not None else None,
            # The UI renders "estimating..." on this rather than a wrong number.
            "estimating": status in UNFINISHED and eta is None,
            "rate_source": src,
            "started_at": _get(row, LANE_COLS[key][0]),
            "ended_at": _get(row, LANE_COLS[key][1]),
            "ok": st["ok"],
            "reason": "disabled" if not en[key] else st["reason"],
            "ran_sec": st["ran_sec"],
        })
    return out


def phases(row: Any, store: Any = None, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Legacy four-entry view, kept shape-compatible for the CRM's existing progress strip.

    The only thing that changed is where a phase's *state* comes from: its owning lane's
    stamps (`PHASE_LANE`), not its index in `PHASE_ORDER`. So 'enriching' and
    'verifying_wa' can both read 'running', which is exactly what is happening.
    """
    now = now or datetime.now(timezone.utc)
    maps_left = _left(_get(row, "maps_deadline_at"), now)
    enabled = phase_enabled(row)
    lane_st = {k: _lane_state(row, k, now) for k in LANE_ORDER}
    lane_en = lane_enabled(row)
    disc_open = lane_en["discovery"] and lane_st["discovery"]["status"] in UNFINISHED

    counts = {
        # total never below done: after an orphan re-queue `links_found` restarts with the
        # new run while `scraped_count` keeps every place saved so far ("77 / 29", job #14).
        "scraping": (int(_get(row, "scraped_count", 0) or 0),
                     _at_least(int(_get(row, "links_found", 0) or 0)
                               or int(_get(row, "max_places", 0) or 0) or None,
                               int(_get(row, "scraped_count", 0) or 0))),
        "enriching": (int(_get(row, "enrich_done", 0) or 0),
                      int(_get(row, "enrich_total", 0) or 0) or None),
        "researching": (int(_get(row, "research_done", 0) or 0),
                        int(_get(row, "research_total", 0) or 0) or None),
        "verifying_wa": (int(_get(row, "wa_verify_done", 0) or 0),
                         int(_get(row, "wa_verify_total", 0) or 0) or None),
    }

    out: list[dict[str, Any]] = []
    for key in [p for p in PHASE_ORDER if enabled[p]]:
        lane = PHASE_LANE[key]
        st = lane_st[lane]
        done, total = counts[key]
        total_is_min = False
        # A phase that has not started yet has no total of its own -- size it from what
        # Maps has found so far, which is what it will be handed.
        if total is None and key in POST_MAPS:
            total = _expected_leads(row)
        if key in POST_MAPS and total is not None and disc_open:
            total_is_min = True

        state = st["status"]
        eta: Optional[float] = None
        src = "done"
        if state in UNFINISHED:
            per, src = _rate(store, key, done, st["started"] if state == "running" else None, now)
            if per is not None and total is not None:
                eta = max(0.0, total - done) * per
            elif key == "scraping" and maps_left is not None and total is None:
                eta, src = maps_left, "budget"
            # Only Maps has a deadline now (see module docstring): capping the post-Maps
            # phases at `enrich_deadline_at` would quote a finish nothing enforces.
            if key == "scraping" and eta is not None and maps_left is not None:
                eta = min(eta, maps_left)

        out.append({
            "key": key,
            "label": LABELS[key],
            "unit": UNITS[key],
            "status": state,
            "done": done,
            "total": total,
            "total_is_min": total_is_min,
            "eta_sec": round(eta) if eta is not None else None,
            # The UI renders "estimating..." on this rather than a wrong number.
            "estimating": state in UNFINISHED and eta is None,
            "rate_source": src,
            # Which thread is doing this work -- lets a consumer group the four entries
            # back into the three lanes without knowing PHASE_LANE.
            "lane": lane,
        })
    return out


def summarise(row: Any, store: Any = None, now: Optional[datetime] = None) -> dict[str, Any]:
    """Everything the UIs need: the legacy phase strip, the three lanes, and a job ETA."""
    now = now or datetime.now(timezone.utc)
    ph = phases(row, store, now)
    ln = lanes(row, store, now)

    # Whole-job ETA = MAX over the unfinished lanes, not the sum: they run concurrently, so
    # the job ends when the slowest one does. Summing was the sequential model's answer and
    # roughly doubled the quoted time. Still all-or-nothing: if any unfinished lane cannot
    # be estimated the job ETA stays null, because a max over a partial set under-reports.
    live = [ln_ for ln_ in ln if ln_["status"] in UNFINISHED]
    total_eta: Optional[int] = None
    if not live:
        total_eta = 0
    elif not any(ln_["estimating"] for ln_ in live):
        total_eta = max(int(ln_["eta_sec"] or 0) for ln_ in live)

    # `phase` / `phase_eta_sec` keep their old meaning for consumers that show one line:
    # the first running phase in the classic order.
    cur = next((p for p in ph if p["status"] == "running"), None)
    disc = next((x for x in ln if x["key"] == "discovery"), None)
    return {
        "phases": ph,
        "lanes": ln,
        "eta_sec": total_eta,
        "phase": cur["key"] if cur else None,
        "phase_eta_sec": cur["eta_sec"] if cur else None,
        "estimating": bool(live) and total_eta is None,
        # Seconds left on the Maps cap while discovery is still running -- the UI shows
        # this as "stops searching Maps in 4m". Nothing else is capped any more, so once
        # discovery is over there is no budget left to report.
        "budget_left_sec": (_left(_get(row, "maps_deadline_at"), now)
                            if disc and disc["status"] == "running" else None),
    }
