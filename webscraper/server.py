"""Local web UI: FastAPI app + a single background worker that runs queued jobs one at a time.

    python -m webscraper serve          # http://127.0.0.1:8765

Jobs are persisted in SQLite (`jobs.phase` drives the UI); the worker thread picks the next
`queued` job, scrapes, optionally enriches, and writes progress counters the page polls.
One Playwright profile = one scrape at a time; the enricher inside a job is concurrent.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

import time
from datetime import datetime, timezone

from webscraper import __version__
from webscraper import eta as eta_mod
from webscraper import lanes as lanes_mod
from webscraper.config import settings
from webscraper.lanes import Lane, Pipeline
from webscraper.store import Store, now_iso

log = logging.getLogger("webscraper.server")
STATIC = Path(__file__).parent / "static"


def _record_rate(store: Store, job_id: int, phase: str, units: Any, started_at: Any) -> None:
    """Bank "phase X did N units in T seconds" so future jobs can be estimated (W4).

    Called as each phase ends. Silent no-op when the phase never started or did nothing —
    `Store.record_phase_rate` drops those rather than let them skew the average.
    """
    try:
        start = datetime.fromisoformat(started_at) if started_at else None
    except (TypeError, ValueError):
        start = None
    if not start:
        return
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    store.record_phase_rate(job_id, phase, int(units or 0),
                            (datetime.now(timezone.utc) - start).total_seconds())


def _col(row, key, default=None):
    """Read a column that may not exist yet (sqlite3.Row raises IndexError, dict KeyError)."""
    try:
        v = row[key]
    except (IndexError, KeyError):
        return default
    return default if v is None else v


def _wall(seconds_from_now: float | None) -> str | None:
    """`n` seconds from now as an ISO instant, for a deadline other processes must see."""
    if not seconds_from_now:
        return None
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)).isoformat(timespec="seconds")

app = FastAPI(title="web-scraper", version=__version__)


# ── run-time window ───────────────────────────────────────────────────────────
def _hhmm(s: str | None) -> int | None:
    """'HH:MM' → minutes since midnight, or None."""
    if not s:
        return None
    try:
        h, m = s.strip().split(":")
        v = int(h) * 60 + int(m)
        return v if 0 <= v < 24 * 60 else None
    except ValueError:
        return None


def in_window(start: str | None, end: str | None, now: datetime | None = None) -> bool:
    """True when local time is inside [start, end). No/invalid window = always. Wraps midnight."""
    a, b = _hhmm(start), _hhmm(end)
    if a is None or b is None or a == b:
        return True
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    return a <= cur < b if a < b else (cur >= a or cur < b)


def _clean_locations(body: "JobIn") -> list[dict[str, Any]]:
    """Normalise JobIn.locations into a clean list; drops empty rows. Falls back to the
    single location/radius/center fields when `locations` is absent."""
    out: list[dict[str, Any]] = []
    for e in (body.locations or []):
        if not isinstance(e, dict):
            continue
        loc = (str(e.get("location") or "")).strip() or None
        lat, lng = e.get("lat"), e.get("lng")
        rad = e.get("radius_km")
        if not (loc or (lat is not None and lng is not None)):
            continue
        out.append({"location": loc, "radius_km": rad,
                    "lat": lat if lat is not None else None,
                    "lng": lng if lng is not None else None})
    if out:
        return out
    loc = (body.location or "").strip() or None
    has_center = body.center_lat is not None and body.center_lng is not None
    if loc or has_center:
        return [{"location": loc, "radius_km": body.radius_km,
                 "lat": body.center_lat if has_center else None,
                 "lng": body.center_lng if has_center else None}]
    return []


def _job_areas(job: Any) -> list[dict[str, Any]]:
    """Normalise a job into a list of scrape areas: {location, radius_km, center}.

    Uses the `locations` JSON list when present (multi-area), else the single
    location/radius/center columns. `center` is (lat, lng) or None.
    """
    import json as _json
    raw = None
    try:
        raw = job["locations"]
    except (KeyError, IndexError, TypeError):
        raw = None
    entries: list[dict[str, Any]] = []
    if raw:
        try:
            parsed = _json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            parsed = None
        for e in (parsed or []):
            if not isinstance(e, dict):
                continue
            lat, lng = e.get("lat"), e.get("lng")
            entries.append({
                "location": (e.get("location") or "").strip() or None,
                "radius_km": e.get("radius_km"),
                "center": (lat, lng) if lat is not None and lng is not None else None,
            })
    if entries:
        return entries
    pinned = (job["center_lat"], job["center_lng"]) if job["center_lat"] is not None else None
    return [{"location": job["location"], "radius_km": job["radius_km"], "center": pinned}]


# ── worker ────────────────────────────────────────────────────────────────────
class Worker(threading.Thread):
    """Sequential job runner. Polls the DB for `phase='queued'` so jobs survive a restart."""

    def __init__(self) -> None:
        super().__init__(name="scrape-worker", daemon=True)
        self.wake = threading.Event()
        self.current_job: int | None = None

    def run(self) -> None:  # noqa: C901 — linear orchestration, fine
        from webscraper.enrich import enrich_places
        from webscraper.maps import Pacing, run_scrape

        while True:
            store = Store()
            # First queued job whose window is open; jobs outside their window show 'waiting'.
            job = None
            for cand in store.queued_jobs():
                if in_window(cand["window_start"], cand["window_end"]):
                    job = cand
                    break
                if cand["phase"] != "waiting":
                    store.update_job(int(cand["id"]), phase="waiting",
                                     message=f"waiting for run window {cand['window_start']}–{cand['window_end']}")
            if job is None:
                store.close()
                self.wake.wait(timeout=2.0)
                self.wake.clear()
                continue
            job_id = int(job["id"])
            self.current_job = job_id
            # ── time budget ─────────────────────────────────────────────────────────
            # `max_minutes` is the GOOGLE MAPS cap, not the whole job (settings.
            # maps_budget_frac defaults to 1.0): "search leads on google maps for 30 mins
            # and then stop that and start research on leads website and linkedin, insta,
            # fb, whatsapp numbers, summary". So the discovery half stops at the cap and
            # the research half then runs on a budget of its own (enrich_budget_frac).
            # Set MAPS_BUDGET_FRAC + ENRICH_BUDGET_FRAC to make the cap cover everything.
            max_minutes = job["max_minutes"]
            bud = eta_mod.budgets(max_minutes)
            t_now = time.monotonic()
            deadline = (t_now + bud["maps_sec"]) if bud["maps_sec"] else None
            # Link collection gets at most 40% of the MAPS budget. A wide radius tiles into
            # thousands of sub-searches, so without this split the collection phase burns
            # the whole limit and the job finishes with links found but zero leads scraped.
            collect_until = (t_now + bud["collect_sec"]) if bud["collect_sec"] else None
            # The deadline goes into the row as wall-clock too: the ETA is computed in the
            # API handler and in the agent loop, neither of which can see this thread's
            # monotonic clock.
            #
            # `enrich_deadline_at` is NO LONGER SET (2026-08-23). Under the lane model
            # `max_minutes` caps discovery only and the other two lanes drain the backlog,
            # so a second deadline would re-create exactly the bug the lanes fixed: on job
            # #6 that budget expired and WhatsApp was cut off after 2 of 30 numbers. The
            # column stays for old rows; nothing writes it any more.
            store.update_job(job_id, maps_deadline_at=_wall(bud["maps_sec"]))
            time_up = {"flag": False}
            # Which Store the discovery closures below talk to. It starts as the worker
            # thread's, and `_discovery` swaps in the discovery LANE's the moment it starts
            # running — because sqlite3 connections are bound to the thread that opened
            # them, and these closures now execute on the lane thread, not this one.
            # Binding `store` as a default argument (the previous shape) captured the
            # worker's connection and raised
            #   "SQLite objects created in a thread can only be used in that same thread"
            # on the first stop check of every job.
            disc = {"store": store}

            def should_stop(j=job_id) -> bool:
                """Discovery's stop predicate: the user's Stop button, or the Maps cap.
                The other two lanes watch only Stop — see lanes.py."""
                s = disc["store"]
                if s.stop_requested(j):
                    return True
                if deadline is not None and time.monotonic() >= deadline:
                    if not time_up["flag"]:
                        time_up["flag"] = True
                        s.update_job(j, message=(
                            f"Maps time limit ({max_minutes} min) reached — discovery stopped; "
                            f"enrichment and WhatsApp keep going on what was found"))
                    return True
                return False

            w_start, w_end = job["window_start"], job["window_end"]

            def wait_if_paused(j=job_id) -> None:
                """Block between places while the run window is closed (checked every 30 s)."""
                s = disc["store"]
                paused = False
                while not in_window(w_start, w_end) and not s.stop_requested(j):
                    if not paused:
                        s.update_job(j, message=f"paused — outside run window {w_start}–{w_end}")
                        paused = True
                    time.sleep(30)
                if paused:
                    s.update_job(j, message="resumed")

            try:
                t0 = now_iso()
                # A re-enrich used to short-circuit here into its own sequential crawl and
                # `continue` — which meant it never reached the lane Pipeline, so WhatsApp
                # verification could NOT run alongside it however the job was configured.
                # Now it just prepares the rows and falls through: DiscoveryLane disables
                # itself when `reenrich_only` is set (no Maps visit), while the enrichment
                # and WhatsApp lanes run concurrently over the scoped leads.
                if job["reenrich_only"]:
                    # Hand the lane its work by resetting the rows worth retrying. Scoped to
                    # `place_keys` when the CRM asked for a subset, so "Re-enrich (24)" is 24
                    # leads and not the whole job.
                    keys = store.job_place_keys(job_id)
                    retry = [r for r in store.places(job_id)
                             if r["enrich_status"] in ("failed", "thin")
                             and (not keys or r["place_key"] in keys)]
                    for r in retry:
                        store.update_enrichment(job_id, r["place_key"], {"enrich_status": "pending"})
                    store.log(job_id, "job",
                              f"re-enrich queued for {len(retry)} lead(s)"
                              + (f" (scoped to {len(keys)})" if keys else " (whole job)"))

                store.update_job(job_id, phase="scraping", status="running", message="opening Google Maps…",
                                 started_at=t0, scrape_started_at=t0)
                pacing = Pacing(delay_sec=float(job["delay_sec"] or settings.delay_sec))
                # How many places the remaining budget could realistically visit. Collecting
                # links beyond that is pure loss - it is time taken away from scraping the
                # ones already found. +30% headroom for links that turn out to be dupes or
                # fall outside the radius on their exact coordinates.
                collect_target = None
                if bud["maps_sec"]:
                    scrape_seconds = bud["maps_sec"] - (bud["collect_sec"] or 0)
                    collect_target = max(50, int(scrape_seconds / max(pacing.delay_sec, 1.0) * 1.3))

                # Multi-location: a job can carry several {location, radius_km, lat, lng} areas.
                # Each area is scraped in turn; counters accumulate; the (job_id, place_key) PK
                # de-dupes a business that shows up in two overlapping areas.
                areas = _job_areas(job)
                agg = {"scraped_base": 0, "links_total": 0}
                area_label = {"txt": ""}

                def on_event(kind: str, data: dict[str, Any]) -> None:
                    # Local rebind: every `store.…` below therefore uses the DISCOVERY
                    # LANE's connection, since this callback fires on that thread.
                    store = disc["store"]
                    pfx = area_label["txt"]
                    if kind == "center":
                        store.update_job(job_id, center_lat=data["lat"], center_lng=data["lng"],
                                         message=f"{pfx}centre {data['lat']:.4f},{data['lng']:.4f}")
                    elif kind == "center_failed":
                        store.update_job(job_id, message=f"{pfx}couldn't resolve the location on Maps — radius ignored")
                    elif kind == "tiles":
                        store.update_job(job_id, message=f"{pfx}radius split into {data['count']} search tiles…")
                    elif kind == "links":
                        tile = f" · tile {data['tile']}/{data['tiles']}" if data.get("tiles", 1) > 1 else ""
                        store.update_job(job_id, links_found=agg["links_total"] + data["count"],
                                         message=f"{pfx}collecting places from the results list{tile}…")
                    elif kind == "links_done":
                        notes = []
                        if data.get("skipped_far"):
                            notes.append(f"{data['skipped_far']} outside radius")
                        if data.get("skipped_known"):
                            notes.append(f"{data['skipped_known']} already in the system")
                        extra = f" ({', '.join(notes)} dropped)" if notes else ""
                        store.update_job(job_id, links_found=agg["links_total"] + data["count"],
                                         skipped_known=data.get("skipped_known", 0),
                                         message=f"{pfx}{data['count']} places found{extra}, opening each one…")
                    elif kind == "place":
                        store.update_job(job_id, scraped_count=agg["scraped_base"] + data["i"])
                    elif kind == "far":
                        store.update_job(job_id, scraped_count=store.get_job(job_id)["scraped_count"] + 1,
                                         skipped_far=data["skipped"],
                                         message=f"{pfx}skipped {data['skipped']} outside radius (last: {data['name']}, {data['distance_km']} km)")
                    elif kind == "captcha":
                        store.update_job(job_id, message=f"{pfx}Google captcha — backing off {data['backoff_sec']:.0f}s")
                    elif kind == "skip":
                        store.update_job(job_id, message=f"{pfx}skipped one ({data['reason']})")
                    elif kind == "links_budget":
                        why = ("collection time up" if data.get("reason") == "time"
                               else "enough places for the time left")
                        store.update_job(job_id, message=(
                            f"{pfx}{why} at tile {data['tile']}/{data['tiles']} — "
                            f"scraping the {data['count']} places found so far"))
                    elif kind == "abort":
                        store.update_job(job_id, message=f"{pfx}{data['reason']}")

                # ── the three lanes ─────────────────────────────────────────────────
                # Discovery's body stays here (it needs these closures); enrichment and
                # WhatsApp run beside it in their own threads, pulling leads out of the
                # `places` table as discovery writes them. See lanes.py for why the table
                # is the queue and why each lane owns its own Store.
                def _discovery(lane: Lane) -> str | None:
                    # This runs on the discovery lane's thread. Point every closure above
                    # at that lane's own Store before touching the DB.
                    disc["store"] = lane.store
                    store = lane.store
                    # "Extend & scrape the pending N": open only the links the capped run
                    # never visited — one pass, no new Maps search (T172).
                    if int(_col(job, "discovery_pending", 0) or 0):
                        from webscraper.maps import FeedCard
                        pend = [FeedCard(href=r["href"], name=r["name"], rating=r["rating"],
                                         reviews_count=r["reviews"], lat=r["lat"], lng=r["lng"])
                                for r in store.pending_links(job_id)]
                        store.update_job(job_id, message=f"opening the {len(pend)} places the last run never reached…")
                        a0 = areas[0]
                        run_scrape(store, job_id, job["query"], a0["location"], 0, pacing,
                                   headless=bool(job["headless"]), country=job["country"],
                                   on_event=on_event, should_stop=should_stop,
                                   radius_km=a0["radius_km"], wait_if_paused=wait_if_paused,
                                   center=a0["center"], known_keys=None,
                                   preset_links=pend)
                        store.update_job(job_id, discovery_pending=0)
                        areas_to_run: list = []
                    else:
                        areas_to_run = list(areas)
                    for idx, area in enumerate(areas_to_run):
                        if should_stop():
                            break
                        area_label["txt"] = (f"area {idx + 1}/{len(areas)} ({area['location'] or 'anywhere'}): "
                                             if len(areas) > 1 else "")
                        known = store.all_place_keys() if job["unique_new"] else None
                        run_scrape(store, job_id, job["query"], area["location"], int(job["max_places"]), pacing,
                                   headless=bool(job["headless"]), country=job["country"],
                                   on_event=on_event, should_stop=should_stop,
                                   radius_km=area["radius_km"], wait_if_paused=wait_if_paused,
                                   center=area["center"], known_keys=known,
                                   collect_until=collect_until, collect_target=collect_target)
                        cur = store.get_job(job_id)
                        agg["scraped_base"] = int(cur["scraped_count"] or 0)
                        agg["links_total"] = int(cur["links_found"] or 0)

                    # Record how fast Maps actually went, so the NEXT job's ETA is grounded
                    # in this machine's real pace instead of a constant (W4).
                    _after_maps = store.get_job(job_id)
                    _record_rate(store, job_id, "scraping", _after_maps["scraped_count"],
                                 _after_maps["scrape_started_at"])
                    if store.stop_requested(job_id):
                        return lanes_mod.R_STOPPED
                    # `time_up` is set by should_stop() the moment the Maps deadline passes,
                    # which is the difference between "found everything" and "ran out of
                    # clock" — previously indistinguishable in the UI.
                    return lanes_mod.R_MAPS_CAP if time_up["flag"] else lanes_mod.R_COMPLETED

                pipe = Pipeline(job_id, dict(job), _discovery)
                reasons = pipe.run()
                log.info("job %s lanes finished: %s", job_id, reasons)

                # push to Supabase (best-effort; no-op when not configured / table missing)
                try:
                    from webscraper import supa
                    if supa.enabled():
                        n = supa.push_job(store, job_id)
                        if n:
                            log.info("pushed %d leads to Supabase for job %s", n, job_id)
                except Exception as e:  # noqa: BLE001
                    log.warning("supabase push error: %s", e)

                # The job is 'stopped' only if the USER stopped it. A lane that hit the Maps
                # cap, the WhatsApp daily cap or its own error still leaves a finished job —
                # the per-lane reason says which, so "2 / 30 · done" can no longer happen.
                user_stopped = store.stop_requested(job_id)
                final = "stopped" if user_stopped else "done"
                note = pipe.summary()
                store.update_job(job_id, phase=final, status=final, message=note)
                store.log(job_id, "job", f"job {final} — {note}")
                store.finish_job(job_id, final, note)
            except Exception as e:  # noqa: BLE001 — keep the worker alive for the next job
                log.exception("job %s failed", job_id)
                store.update_job(job_id, phase="failed", status="failed", message=str(e)[:300])
                store.finish_job(job_id, "failed", str(e)[:300])
            finally:
                self.current_job = None
                store.close()


worker = Worker()


@app.on_event("startup")
def _start_worker() -> None:
    # A job that was mid-run when the server died is re-queued, not lost.
    s = Store()
    s.conn.execute("UPDATE jobs SET phase='queued', stop_requested=0 WHERE phase IN ('scraping','enriching')")
    s.conn.commit()
    s.close()
    if not worker.is_alive():
        worker.start()


# ── api ───────────────────────────────────────────────────────────────────────
class JobIn(BaseModel):
    query: Optional[str] = Field(None, max_length=400)   # single term; or use `keywords`
    location: Optional[str] = Field(None, max_length=120)
    max_places: int = Field(50, ge=0, le=100000)   # 0 = unlimited (everything Maps returns / all tiles)
    delay_sec: float = Field(settings.delay_sec, ge=1, le=120)
    headless: bool = settings.headless
    enrich: bool = True
    # "" / None = auto: per-lead country from the Maps address, job falls back to DEFAULT_COUNTRY
    country: Optional[str] = Field(None, max_length=2)
    radius_km: Optional[float] = Field(None, ge=0.3, le=300)
    window_start: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")   # local HH:MM
    window_end: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    center_lat: Optional[float] = Field(None, ge=-90, le=90)    # pinned on the map picker
    center_lng: Optional[float] = Field(None, ge=-180, le=180)
    max_minutes: Optional[int] = Field(None, ge=1, le=24 * 60)   # stop scraping N minutes after start
    draft: bool = False                                          # save without queueing
    unique_new: bool = False                                     # skip places already scraped by any job
    research: bool = False                                       # AI summary / owner / team pass
    wa_verify: bool = False                                      # check each number against WhatsApp Web
    keywords: Optional[list[str]] = None                         # multiple search terms (joined into query)
    # Multi-area: [{location, radius_km, lat, lng}]. When set (len>1) it wins over the single
    # location/radius above; the first entry is mirrored into those columns for back-compat.
    locations: Optional[list[dict[str, Any]]] = None


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _job_dict(r: Any, store: Store | None = None) -> dict[str, Any]:
    """Row → dict plus derived `running`, `elapsed_sec`, and the phase/ETA block.

    `store` is optional only so callers that already closed theirs still work; without
    it there is no rate history, so every ETA comes back as "estimating".
    """
    d = dict(r)
    phase = d.get("phase")
    d["running"] = phase in ("scraping", "enriching", "researching", "verifying_wa")
    d["window_open"] = in_window(d.get("window_start"), d.get("window_end"))
    now = datetime.now(timezone.utc)
    started = _ts(d.get("started_at")) or (_ts(d.get("created_at")) if phase != "queued" else None)
    finished = _ts(d.get("finished_at")) if phase in ("done", "stopped", "failed") else None
    d["elapsed_sec"] = round(((finished or now) - started).total_seconds()) if started else None

    # Phases + ETA come from the shared model (webscraper/eta.py) so the local UI, the
    # SaaS cloud and the CRM all render the same figures from the same rolling averages.
    summary = eta_mod.summarise(d, store, now)
    d["phases"] = summary["phases"]
    # The three concurrent lanes, with each one's runtime, end reason and success flag.
    # Forwarded so the page renders the SAME numbers the Python model computed — without
    # this the UI falls back to re-deriving lanes from raw columns in JS, i.e. a second
    # copy of this logic that can drift.
    d["lanes"] = summary["lanes"]
    d["eta_sec"] = summary["eta_sec"]
    d["phase_eta_sec"] = summary["phase_eta_sec"]
    d["estimating"] = summary["estimating"]
    # Seconds left on the budget capping the CURRENT phase — for scraping that is the
    # Maps cap the user set, after which discovery hands over to the research phases.
    d["time_left_sec"] = (round(summary["budget_left_sec"])
                          if summary["budget_left_sec"] is not None else None)
    return d


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "version": __version__, "worker_alive": worker.is_alive(),
            "current_job": worker.current_job}


@app.post("/api/jobs")
def create_job(body: JobIn) -> dict[str, Any]:
    s = Store()
    try:
        locs = _clean_locations(body)
        primary = locs[0] if locs else {"location": (body.location or "").strip() or None,
                                        "radius_km": body.radius_km, "lat": None, "lng": None}
        loc = primary["location"]
        has_center = primary.get("lat") is not None and primary.get("lng") is not None
        if primary.get("radius_km") and not (loc or has_center):
            raise HTTPException(422, "radius needs a location (type one or pick it on the map)")
        win = (body.window_start, body.window_end) if (body.window_start and body.window_end) else (None, None)
        query = ", ".join(k.strip() for k in body.keywords if k.strip()) if body.keywords else (body.query or '').strip()
        if not query:
            raise HTTPException(422, "at least one keyword is required")
        job_id = s.create_job(query, loc, body.max_places,
                              body.delay_sec, phase="draft" if body.draft else "queued",
                              do_enrich=body.enrich, headless=body.headless,
                              country=(body.country or "").strip().upper() or None,
                              radius_km=primary.get("radius_km"), window_start=win[0], window_end=win[1],
                              center_lat=primary.get("lat") if has_center else None,
                              center_lng=primary.get("lng") if has_center else None,
                              max_minutes=body.max_minutes, unique_new=body.unique_new)
        s.update_job(job_id, do_research=int(body.research), do_wa_verify=int(body.wa_verify),
                     locations=json.dumps(locs) if len(locs) > 1 else None,
                     message="draft" if body.draft else "queued",
                     status="draft" if body.draft else "running")
        job = _job_dict(s.get_job(job_id), s)
    finally:
        s.close()
    if not body.draft:
        worker.wake.set()
    return job


@app.patch("/api/jobs/{job_id}")
def update_draft(job_id: int, body: JobIn) -> dict[str, Any]:
    """Edit a draft's fields (drafts only)."""
    s = Store()
    try:
        r = s.get_job(job_id)
        if not r:
            raise HTTPException(404, "no such job")
        if r["phase"] != "draft":
            raise HTTPException(409, "only drafts can be edited")
        locs = _clean_locations(body)
        primary = locs[0] if locs else {"location": (body.location or "").strip() or None,
                                        "radius_km": body.radius_km, "lat": None, "lng": None}
        has_center = primary.get("lat") is not None and primary.get("lng") is not None
        win = (body.window_start, body.window_end) if (body.window_start and body.window_end) else (None, None)
        query = ", ".join(k.strip() for k in body.keywords if k.strip()) if body.keywords else (body.query or '').strip()
        s.update_job(job_id, query=query or (body.query or '').strip(), location=primary["location"],
                     do_research=int(body.research), do_wa_verify=int(body.wa_verify),
                     locations=json.dumps(locs) if len(locs) > 1 else None,
                     max_places=body.max_places, delay_sec=body.delay_sec,
                     do_enrich=int(body.enrich), headless=int(body.headless),
                     country=(body.country or "").strip().upper() or None,
                     radius_km=primary.get("radius_km"), window_start=win[0], window_end=win[1],
                     center_lat=primary.get("lat") if has_center else None,
                     center_lng=primary.get("lng") if has_center else None,
                     max_minutes=body.max_minutes, unique_new=int(body.unique_new))
        return _job_dict(s.get_job(job_id), s)
    finally:
        s.close()


@app.post("/api/jobs/{job_id}/start")
def start_draft(job_id: int) -> dict[str, Any]:
    """Queue a draft."""
    s = Store()
    try:
        r = s.get_job(job_id)
        if not r:
            raise HTTPException(404, "no such job")
        if r["phase"] != "draft":
            raise HTTPException(409, "job is not a draft")
        if r["radius_km"] and not (r["location"] or r["center_lat"] is not None):
            raise HTTPException(422, "radius needs a location")
        s.update_job(job_id, phase="queued", status="running", stop_requested=0, message="queued")
        job = _job_dict(s.get_job(job_id), s)
    finally:
        s.close()
    worker.wake.set()
    return job


@app.get("/api/jobs")
def list_jobs() -> list[dict[str, Any]]:
    s = Store()
    try:
        return [_job_dict(r, s) for r in s.list_jobs()]
    finally:
        s.close()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int) -> dict[str, Any]:
    s = Store()
    try:
        r = s.get_job(job_id)
        if not r:
            raise HTTPException(404, "no such job")
        d = _job_dict(r, s)
        d["places_count"] = s.conn.execute("SELECT COUNT(*) FROM places WHERE job_id=?", (job_id,)).fetchone()[0]
        return d
    finally:
        s.close()


@app.get("/api/jobs/{job_id}/places")
def job_places(job_id: int) -> list[dict[str, Any]]:
    s = Store()
    try:
        rows = s.places(job_id)
        for r in rows:
            r.pop("raw", None)
        return rows
    finally:
        s.close()


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: int) -> dict[str, Any]:
    s = Store()
    try:
        r = s.get_job(job_id)
        if not r:
            raise HTTPException(404, "no such job")
        if r["phase"] in ("queued", "waiting"):
            s.update_job(job_id, phase="stopped", status="stopped", message="cancelled before start")
        else:
            s.update_job(job_id, stop_requested=1, message="stopping after current place…")
        return _job_dict(s.get_job(job_id), s)
    finally:
        s.close()


@app.post("/api/jobs/{job_id}/reenrich")
def reenrich_job(job_id: int) -> dict[str, Any]:
    """Re-queue a finished job to retry enrichment on failed / thin / pending rows."""
    s = Store()
    try:
        r = s.get_job(job_id)
        if not r:
            raise HTTPException(404, "no such job")
        if r["phase"] in ("scraping", "enriching", "queued", "waiting"):
            raise HTTPException(409, "job is still running")
        n = sum(1 for p in s.places(job_id) if p["enrich_status"] in ("failed", "thin", "pending"))
        if n == 0:
            raise HTTPException(400, "nothing to re-enrich")
        s.update_job(job_id, phase="queued", status="running", reenrich_only=1, stop_requested=0,
                     message=f"queued: retry {n} websites")
        job = _job_dict(s.get_job(job_id), s)
    finally:
        s.close()
    worker.wake.set()
    return job


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: int) -> dict[str, Any]:
    s = Store()
    try:
        r = s.get_job(job_id)
        if not r:
            raise HTTPException(404, "no such job")
        if r["phase"] in ("scraping", "enriching", "queued", "waiting"):
            raise HTTPException(409, "stop the job first")
        s.conn.execute("DELETE FROM places WHERE job_id=?", (job_id,))
        s.conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        s.conn.commit()
        return {"deleted": job_id}
    finally:
        s.close()


@app.get("/api/jobs/{job_id}/export")
def export_job(job_id: int, fmt: str = "xlsx", unique: bool = False) -> FileResponse:
    """`unique=1` collapses branches of the same website (chains) into one lead."""
    s = Store()
    try:
        if not s.get_job(job_id):
            raise HTTPException(404, "no such job")
        if fmt == "xlsx":
            path = s.export_xlsx(job_id, unique=unique)
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif fmt in ("csv", "json"):
            path = s.export(job_id, fmt, unique=unique)
            media = "text/csv" if fmt == "csv" else "application/json"
        else:
            raise HTTPException(400, "fmt must be xlsx|csv|json")
    finally:
        s.close()
    return FileResponse(path, media_type=media, filename=path.name)


# ── AI keyword suggestions ────────────────────────────────────────────────────
import os

_SUGGEST_PROMPT = (
    "You help build Google Maps lead-generation searches. For the business type {q!r}, list 10 other "
    "Google Maps search keywords that find the same or closely related businesses (synonyms, specialisations, "
    "adjacent services people would also target). Reply as a JSON array of short strings only, no prose."
)


@app.get("/api/suggest")
def suggest_keywords(q: str) -> dict[str, Any]:
    """Related search keywords for a business type. Uses Groq/OpenAI when a key is in .env,
    otherwise falls back to Google's public autosuggest."""
    import json as _json

    import httpx

    q = (q or "").strip()
    if len(q) < 2:
        return {"source": "none", "keywords": []}
    gemini = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_AI_STUDIO_API_KEY")
    groq, openai = os.getenv("GROQ_API_KEY"), os.getenv("OPENAI_API_KEY")
    prompt = _SUGGEST_PROMPT.format(q=q)
    try:
        if gemini:
            r = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={gemini}",
                timeout=20,
                json={"contents": [{"parts": [{"text": prompt}]}],
                      # thinkingBudget 0: Gemini 2.5's hidden thinking tokens otherwise eat the
                      # output cap and the text comes back empty
                      "generationConfig": {"temperature": 0.4, "maxOutputTokens": 1000,
                                           "responseMimeType": "application/json",
                                           "thinkingConfig": {"thinkingBudget": 0}}})
            r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            m = text[text.find("["): text.rfind("]") + 1]
            kws = [str(x).strip() for x in _json.loads(m) if str(x).strip()][:12]
            if kws:
                return {"source": "gemini", "keywords": kws}
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
        log.warning("Gemini suggest failed, falling back: %s", e)
    try:
        if groq or openai:
            url = ("https://api.groq.com/openai/v1/chat/completions" if groq
                   else "https://api.openai.com/v1/chat/completions")
            model = "llama-3.1-8b-instant" if groq else "gpt-4o-mini"
            r = httpx.post(url, timeout=20,
                           headers={"Authorization": f"Bearer {groq or openai}"},
                           json={"model": model, "temperature": 0.4, "max_tokens": 300,
                                 "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
            m = text[text.find("["): text.rfind("]") + 1]
            kws = [str(x).strip() for x in _json.loads(m) if str(x).strip()][:12]
            if kws:
                return {"source": "groq" if groq else "openai", "keywords": kws}
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
        log.warning("LLM suggest failed, falling back: %s", e)
    # Fallback: Google autosuggest around the term (free, no key)
    kws: list[str] = []
    try:
        for seed in (q, f"{q} near", f"best {q}"):
            r = httpx.get("https://suggestqueries.google.com/complete/search",
                          params={"client": "firefox", "q": seed}, timeout=10,
                          headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                for s_ in r.json()[1]:
                    s_ = s_.strip()
                    if s_ and s_.lower() != q.lower() and s_ not in kws:
                        kws.append(s_)
    except (httpx.HTTPError, ValueError, IndexError):
        pass
    return {"source": "autosuggest", "keywords": kws[:12]}


# ── geocoding for the map picker (OpenStreetMap Nominatim; proxied so we send a proper UA) ──
_NOMINATIM = "https://nominatim.openstreetmap.org"
_GEO_HEADERS = {"User-Agent": f"web-scraper/{__version__} (local lead scraper; contact: local user)",
                "Accept-Language": "en"}


@app.get("/api/geocode")
def geocode(q: str) -> list[dict[str, Any]]:
    import httpx

    q = (q or "").strip()
    if len(q) < 2:
        return []
    try:
        r = httpx.get(f"{_NOMINATIM}/search", params={"q": q, "format": "jsonv2", "limit": 6, "addressdetails": 1},
                      headers=_GEO_HEADERS, timeout=12)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"geocoder unavailable: {e}") from e
    out = []
    for it in r.json():
        addr = it.get("address") or {}
        short = ", ".join(x for x in (
            addr.get("suburb") or addr.get("neighbourhood") or addr.get("city_district") or addr.get("town") or addr.get("village"),
            addr.get("city") or addr.get("county") or addr.get("state_district"),
            addr.get("state"), addr.get("country")) if x)
        out.append({"name": it.get("display_name"), "short": short or it.get("display_name"),
                    "lat": float(it["lat"]), "lng": float(it["lon"]),
                    "bbox": [float(x) for x in it.get("boundingbox", [])] or None})
    return out


@app.get("/api/revgeocode")
def revgeocode(lat: float, lng: float) -> dict[str, Any]:
    import httpx

    try:
        r = httpx.get(f"{_NOMINATIM}/reverse", params={"lat": lat, "lon": lng, "format": "jsonv2", "zoom": 14},
                      headers=_GEO_HEADERS, timeout=12)
        r.raise_for_status()
        it = r.json()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"geocoder unavailable: {e}") from e
    addr = it.get("address") or {}
    short = ", ".join(x for x in (
        addr.get("suburb") or addr.get("neighbourhood") or addr.get("city_district") or addr.get("town") or addr.get("village"),
        addr.get("city") or addr.get("county") or addr.get("state_district"),
        addr.get("state"), addr.get("country")) if x)
    return {"name": it.get("display_name"), "short": short or it.get("display_name") or f"{lat:.4f}, {lng:.4f}"}


@app.get("/api/leads")
def all_leads(unique: bool = True, limit: int = 2000, source: str = "local") -> dict[str, Any]:
    """Every scraped lead. source=local (SQLite, unique per place) or supabase (cloud table)."""
    if source == "supabase":
        from webscraper import supa
        rows = supa.fetch_leads(limit=max(1, min(limit, 10000)))
        return {"total": len(rows), "rows": rows, "source": "supabase"}
    s = Store()
    try:
        rows = s.leads_all(unique)
        total = len(rows)
        rows = rows[:max(1, min(limit, 10000))]
        for r in rows:
            r.pop("raw", None)
        return {"total": total, "rows": rows, "source": "local"}
    finally:
        s.close()


@app.get("/api/supabase/status")
def supabase_status() -> dict[str, Any]:
    from webscraper import supa
    return supa.status()


@app.post("/api/supabase/setup")
def supabase_setup() -> dict[str, Any]:
    """Auto-create the Supabase table over direct Postgres (needs SUPABASE_DB_PASS)."""
    from webscraper import supa
    if not supa.enabled():
        raise HTTPException(400, "Supabase not configured in .env")
    if supa.status().get("table_exists"):
        return {"created": False, "already": True}
    if not supa.create_table_via_pg():
        raise HTTPException(502, "couldn't auto-create — paste the setup SQL in the Supabase SQL editor")
    return {"created": True}


@app.post("/api/supabase/sync")
def supabase_sync() -> dict[str, Any]:
    """Push every local lead to Supabase (upsert). Auto-creates the table if missing."""
    from webscraper import supa
    if not supa.enabled():
        raise HTTPException(400, "Supabase not configured in .env")
    st = supa.status()
    if not st.get("table_exists"):
        supa.create_table_via_pg()
        if not supa.status().get("table_exists"):
            raise HTTPException(409, "table missing — paste the setup SQL in the Supabase SQL editor")
    s = Store()
    try:
        n = supa.push_rows(s.leads_all(unique=True))
    finally:
        s.close()
    return {"pushed": n}


@app.get("/api/leads/export")
def export_leads(fmt: str = "xlsx", unique: bool = True) -> FileResponse:
    if fmt not in ("xlsx", "csv", "json"):
        raise HTTPException(400, "fmt must be xlsx|csv|json")
    s = Store()
    try:
        path = s.export_leads(fmt, unique)
    finally:
        s.close()
    media = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
             "csv": "text/csv", "json": "application/json"}[fmt]
    return FileResponse(path, media_type=media, filename=path.name)


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    s = Store()
    try:
        return s.stats()
    finally:
        s.close()


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    import webbrowser

    import uvicorn

    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://{host}:{port}/")).start()
    uvicorn.run(app, host=host, port=port, log_level="info")
