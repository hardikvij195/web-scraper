"""Phase model + estimated time remaining for a job (T136 / W2 + W4).

A job is a chain of phases against one clock:

    collect links ─┐
                   ├─ "scraping"  (Google Maps)   ← capped by jobs.maps_deadline_at
    open places  ──┘
    enriching     (website, LinkedIn, Instagram, Facebook, WhatsApp)  ┐
    researching   (AI summary / owner / team)                        ├─ capped by
    verifying_wa  (is the number really on WhatsApp)                 ┘  enrich_deadline_at

`max_minutes` is the **Maps** cap by default (settings.maps_budget_frac = 1.0) — the user
asked for "search leads on google maps for 30 mins and then stop that and start research".
The post-Maps phases get their own budget (settings.enrich_budget_frac of max_minutes).

Everything here is pure read-side: it takes a jobs row plus a Store (for the rolling
per-phase rates) and returns what to display. Both the local API (`server._job_dict`) and
the cloud/CRM agent (`agent._local_progress`) call it, so the local UI and the CRM show
the same numbers.

**No hardcoded rate constants.** A phase's seconds-per-unit comes from (a) what this very
run has already done, once there are enough samples for it to mean anything, else (b) a
rolling average over the last N runs of that phase on this machine, else (c) nothing —
and "nothing" is reported as `estimating: true`, never as a made-up number.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from webscraper.config import settings

# Order matters: it is the order the worker runs them in, and the order the UI lists them.
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

# Phases capped by the *second* budget rather than the Maps one.
POST_MAPS = ("enriching", "researching", "verifying_wa")

# How many units the current run must have finished before its own observed pace is
# trusted over the historical average. Scraping is metronomic (fixed delay per place) so
# 3 is plenty; the enricher is concurrent and site-dependent, so it needs a longer look.
MIN_LIVE_SAMPLES = {"scraping": 3, "enriching": 5, "researching": 3, "verifying_wa": 3}

# ...and the phase must also have been running for this long. Counters can be non-zero
# the instant a phase's start stamp is (re)written — a resumed job, or a phase that
# inherits work already done — which would compute a near-zero seconds-per-unit and
# promise a finish that is seconds away. Below this, fall back to the history.
MIN_LIVE_ELAPSED_SEC = 20.0

TERMINAL = ("done", "stopped", "failed", "cancelled", "error")


def _get(row: Any, key: str, default: Any = None) -> Any:
    """Read a column from an sqlite3.Row or a plain dict without blowing up on absence."""
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
    shape to deal with.
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
    """How many businesses the post-Maps phases will have to chew through.

    Before enrichment starts nothing has counted them yet, so fall back to what Maps
    found (or is going to find). An unlimited job (max_places = 0) before any link has
    been collected is genuinely unknowable — return None and let the caller say so.
    """
    for key in ("enrich_total", "links_found", "scraped_count", "max_places"):
        v = int(_get(row, key, 0) or 0)
        if v > 0:
            return v
    return None


def phases(row: Any, store: Any = None, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Per-phase status + ETA for one job row. Only the phases this job will actually run."""
    now = now or datetime.now(timezone.utc)
    status = str(_get(row, "phase", "") or _get(row, "status", "") or "")
    finished = status in TERMINAL
    maps_left = _left(_get(row, "maps_deadline_at"), now)
    enrich_left = _left(_get(row, "enrich_deadline_at"), now)

    # A wa_verify_only re-run touches nothing but WhatsApp — showing it a greyed-out
    # "Google Maps discovery" bar would be a lie about what the job is doing.
    wa_only = bool(_get(row, "wa_verify_only", 0))
    enabled = {
        "scraping": not wa_only,
        "enriching": bool(_get(row, "do_enrich", 1)) and not wa_only,
        "researching": bool(_get(row, "do_research", 0)) and not wa_only,
        "verifying_wa": bool(_get(row, "do_wa_verify", 0)) or wa_only,
    }
    active = [p for p in PHASE_ORDER if enabled[p]]
    cur_idx = active.index(status) if status in active else None

    counts = {
        "scraping": (int(_get(row, "scraped_count", 0) or 0),
                     int(_get(row, "links_found", 0) or 0) or int(_get(row, "max_places", 0) or 0) or None,
                     _ts(_get(row, "scrape_started_at"))),
        "enriching": (int(_get(row, "enrich_done", 0) or 0),
                      int(_get(row, "enrich_total", 0) or 0) or None,
                      _ts(_get(row, "enrich_started_at"))),
        "researching": (int(_get(row, "research_done", 0) or 0),
                        int(_get(row, "research_total", 0) or 0) or None,
                        _ts(_get(row, "research_started_at"))),
        "verifying_wa": (int(_get(row, "wa_verify_done", 0) or 0),
                         int(_get(row, "wa_verify_total", 0) or 0) or None,
                         _ts(_get(row, "wa_started_at"))),
    }

    out: list[dict[str, Any]] = []
    for i, key in enumerate(active):
        done, total, started = counts[key]
        # A phase that has not started yet has no total of its own — size it from what
        # Maps has found so far, which is what it will be handed.
        if total is None and key in POST_MAPS:
            total = _expected_leads(row)

        if finished:
            state = "done" if status == "done" else status
        elif cur_idx is None:
            state = "pending"           # queued / waiting / draft: nothing has started
        elif i < cur_idx:
            state = "done"
        elif i == cur_idx:
            state = "running"
        else:
            state = "pending"

        eta: Optional[float] = None
        src = "done"
        if state in ("running", "pending"):
            per, src = _rate(store, key, done, started if state == "running" else None, now)
            if per is not None and total is not None:
                eta = max(0.0, total - done) * per
            elif key == "scraping" and maps_left is not None and total is None:
                # Unlimited discovery: the only honest estimate is "until the cap".
                eta = maps_left
                src = "budget"
            # A phase can never outrun the budget that governs it.
            cap = maps_left if key == "scraping" else enrich_left
            if eta is not None and cap is not None:
                eta = min(eta, cap)

        out.append({
            "key": key,
            "label": LABELS[key],
            "unit": UNITS[key],
            "status": state,
            "done": done,
            "total": total,
            "eta_sec": round(eta) if eta is not None else None,
            # The UI renders "estimating…" on this rather than a wrong number.
            "estimating": state in ("running", "pending") and eta is None,
            "rate_source": src,
        })
    return out


def summarise(row: Any, store: Any = None, now: Optional[datetime] = None) -> dict[str, Any]:
    """Everything the UIs need to draw the phase strip: phases, whole-job ETA, budget left."""
    now = now or datetime.now(timezone.utc)
    ph = phases(row, store, now)
    remaining = [p for p in ph if p["status"] in ("running", "pending")]
    # Whole-job ETA only when EVERY remaining phase could be estimated. Adding up a
    # partial sum would under-report, and that is worse than admitting we don't know yet.
    total_eta: Optional[int] = None
    if remaining and not any(p["estimating"] for p in remaining):
        total_eta = sum(int(p["eta_sec"] or 0) for p in remaining)
    elif not remaining:
        total_eta = 0
    cur = next((p for p in ph if p["status"] == "running"), None)
    return {
        "phases": ph,
        "eta_sec": total_eta,
        "phase": cur["key"] if cur else None,
        "phase_eta_sec": cur["eta_sec"] if cur else None,
        "estimating": bool(remaining) and total_eta is None,
        # Seconds left on the budget governing the current phase — the UI shows this as
        # "stops searching Maps in 4m", which is a different thing from the ETA.
        "budget_left_sec": (
            _left(_get(row, "maps_deadline_at"), now) if (cur and cur["key"] == "scraping")
            else _left(_get(row, "enrich_deadline_at"), now) if cur else None
        ),
    }
