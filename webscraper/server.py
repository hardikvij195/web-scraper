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
from webscraper.config import settings
from webscraper.store import Store, now_iso

log = logging.getLogger("webscraper.server")
STATIC = Path(__file__).parent / "static"

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
            # Time limit: deadline is computed from the moment the job actually starts.
            max_minutes = job["max_minutes"]
            deadline = (time.monotonic() + max_minutes * 60) if max_minutes else None
            time_up = {"flag": False}

            def should_stop(s=store, j=job_id) -> bool:
                if s.stop_requested(j):
                    return True
                if deadline is not None and time.monotonic() >= deadline:
                    if not time_up["flag"]:
                        time_up["flag"] = True
                        s.update_job(j, message=f"time limit ({max_minutes} min) reached — keeping what was collected")
                    return True
                return False

            w_start, w_end = job["window_start"], job["window_end"]

            def wait_if_paused(s=store, j=job_id) -> None:
                """Block between places while the run window is closed (checked every 30 s)."""
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
                if job["reenrich_only"]:
                    # Retry the website crawl only (failed / thin / still-pending rows); no Maps visit.
                    rows = [r for r in store.places(job_id) if r["enrich_status"] in ("failed", "thin", "pending")]
                    store.update_job(job_id, phase="enriching", status="running", started_at=t0,
                                     enrich_started_at=t0, enrich_total=len(rows), enrich_done=0,
                                     message=f"re-crawling {len(rows)} websites…")
                    done = {"n": 0}

                    def on_progress2(_r: dict[str, Any], _status: str) -> None:
                        done["n"] += 1
                        store.update_job(job_id, enrich_done=done["n"])

                    asyncio.run(enrich_places(store, rows, None, job["country"], on_progress2, should_stop))
                    final = "stopped" if should_stop() else "done"
                    store.update_job(job_id, phase=final, status=final, message=None, reenrich_only=0)
                    store.finish_job(job_id, final)
                    continue

                store.update_job(job_id, phase="scraping", status="running", message="opening Google Maps…",
                                 started_at=t0, scrape_started_at=t0)
                pacing = Pacing(delay_sec=float(job["delay_sec"] or settings.delay_sec))

                # Multi-location: a job can carry several {location, radius_km, lat, lng} areas.
                # Each area is scraped in turn; counters accumulate; the (job_id, place_key) PK
                # de-dupes a business that shows up in two overlapping areas.
                areas = _job_areas(job)
                agg = {"scraped_base": 0, "links_total": 0}
                area_label = {"txt": ""}

                def on_event(kind: str, data: dict[str, Any]) -> None:
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
                    elif kind == "abort":
                        store.update_job(job_id, message=f"{pfx}{data['reason']}")

                for idx, area in enumerate(areas):
                    if should_stop():
                        break
                    area_label["txt"] = (f"area {idx + 1}/{len(areas)} ({area['location'] or 'anywhere'}): "
                                         if len(areas) > 1 else "")
                    known = store.all_place_keys() if job["unique_new"] else None
                    run_scrape(store, job_id, job["query"], area["location"], int(job["max_places"]), pacing,
                               headless=bool(job["headless"]), country=job["country"],
                               on_event=on_event, should_stop=should_stop,
                               radius_km=area["radius_km"], wait_if_paused=wait_if_paused,
                               center=area["center"], known_keys=known)
                    cur = store.get_job(job_id)
                    agg["scraped_base"] = int(cur["scraped_count"] or 0)
                    agg["links_total"] = int(cur["links_found"] or 0)

                # A user Stop ends the job here; a time limit only ends the *scraping* — what was
                # collected still gets enriched so the leads are complete.
                if store.stop_requested(job_id):
                    store.update_job(job_id, phase="stopped", status="stopped")
                    continue
                user_stop = lambda s=store, j=job_id: s.stop_requested(j)  # noqa: E731

                if job["do_enrich"]:
                    rows = store.places(job_id, "pending")
                    store.update_job(job_id, phase="enriching", enrich_total=len(rows), enrich_done=0,
                                     enrich_started_at=now_iso(),
                                     message=f"crawling {len(rows)} websites for email / socials / WhatsApp…")
                    done = {"n": 0}

                    def on_progress(_r: dict[str, Any], _status: str) -> None:
                        done["n"] += 1
                        if done["n"] % 3 == 0 or done["n"] == len(rows):
                            store.update_job(job_id, enrich_done=done["n"])

                    asyncio.run(enrich_places(store, rows, None, job["country"], on_progress, user_stop))
                    store.update_job(job_id, enrich_done=done["n"])

                if job["do_research"] and not user_stop():
                    from webscraper.research import research_places
                    targets = [p for p in store.places(job_id) if p.get("website")]
                    store.update_job(job_id, phase="researching", research_total=len(targets), research_done=0,
                                     message=f"AI research on {len(targets)} businesses (summary / owner / team)…")
                    rdone = {"n": 0}

                    def on_research(_r: dict[str, Any], _s: str) -> None:
                        rdone["n"] += 1
                        if rdone["n"] % 2 == 0 or rdone["n"] == len(targets):
                            store.update_job(job_id, research_done=rdone["n"])

                    rc = asyncio.run(research_places(store, targets, 3, on_research, user_stop))
                    store.update_job(job_id, research_done=rdone["n"])
                    if rc.get("skipped") == len(targets) and targets:
                        store.update_job(job_id, message="AI research skipped — set GEMINI_API_KEY in .env")

                if job["do_wa_verify"] and not user_stop():
                    from webscraper import wa_verify
                    targets = [p for p in store.places(job_id)
                               if (p.get("phone") or p.get("whatsapp_number"))]
                    store.update_job(job_id, phase="verifying_wa", wa_verify_total=len(targets),
                                     wa_verify_done=0,
                                     message=f"checking {len(targets)} numbers on WhatsApp (paced)…")
                    vdone = {"n": 0}

                    def on_wa(_pk: str, _s: str) -> None:
                        vdone["n"] += 1
                        store.update_job(job_id, wa_verify_done=vdone["n"])

                    try:
                        res = wa_verify.verify_places(store, targets, on_wa, user_stop, job_id=job_id)
                        note = (f"WhatsApp: {res['yes']} on WA, {res['no']} not, "
                                f"{res['unknown']} unknown")
                        if res["capped"]:
                            note += " · daily cap hit — re-run to finish the rest"
                        store.update_job(job_id, message=note)
                    except wa_verify.WaNotLoggedIn as e:
                        store.update_job(job_id, message=f"WhatsApp verify skipped — {e}")

                # push to Supabase (best-effort; no-op when not configured / table missing)
                try:
                    from webscraper import supa
                    if supa.enabled():
                        n = supa.push_job(store, job_id)
                        if n:
                            log.info("pushed %d leads to Supabase for job %s", n, job_id)
                except Exception as e:  # noqa: BLE001
                    log.warning("supabase push error: %s", e)

                final = "stopped" if user_stop() else "done"
                note = f"time limit {max_minutes} min reached" if time_up["flag"] else None
                store.update_job(job_id, phase=final, status=final, message=note)
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


PLACE_OVERHEAD_SEC = 3.5      # page load + parse on top of the configured delay
ENRICH_SEC_PER_SITE = 0.9     # observed average at concurrency 5


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _job_dict(r: Any) -> dict[str, Any]:
    """Row → dict plus derived `running`, `elapsed_sec`, `eta_sec` (None when unknown)."""
    d = dict(r)
    phase = d.get("phase")
    d["running"] = phase in ("scraping", "enriching", "researching", "verifying_wa")
    d["window_open"] = in_window(d.get("window_start"), d.get("window_end"))
    now = datetime.now(timezone.utc)
    started = _ts(d.get("started_at")) or (_ts(d.get("created_at")) if phase != "queued" else None)
    finished = _ts(d.get("finished_at")) if phase in ("done", "stopped", "failed") else None
    d["elapsed_sec"] = round(((finished or now) - started).total_seconds()) if started else None

    eta: float | None = None
    delay = float(d.get("delay_sec") or settings.delay_sec)
    per_place_guess = delay + PLACE_OVERHEAD_SEC
    links = int(d.get("links_found") or 0)
    scraped = int(d.get("scraped_count") or 0)
    max_places = int(d.get("max_places") or 0)
    enrich_on = bool(d.get("do_enrich"))
    if phase in ("queued", "waiting"):
        guess = max_places or 120       # unlimited: assume one Maps page until links are counted
        eta = guess * per_place_guess + (guess * ENRICH_SEC_PER_SITE if enrich_on else 0)
    elif phase == "scraping":
        total = links or max_places or 120
        ss = _ts(d.get("scrape_started_at"))
        per = ((now - ss).total_seconds() / scraped) if (ss and scraped >= 2) else per_place_guess
        eta = max(0, total - scraped) * per + (total * ENRICH_SEC_PER_SITE if enrich_on else 0)
    elif phase == "enriching":
        total = int(d.get("enrich_total") or 0)
        done = int(d.get("enrich_done") or 0)
        es = _ts(d.get("enrich_started_at"))
        per = ((now - es).total_seconds() / done) if (es and done >= 3) else ENRICH_SEC_PER_SITE
        eta = max(0, total - done) * per
    # time limit: seconds left until scraping stops (None when no limit / not started)
    d["time_left_sec"] = None
    if d.get("max_minutes") and started and phase in ("scraping",):
        left = d["max_minutes"] * 60 - (now - started).total_seconds()
        d["time_left_sec"] = max(0, round(left))
        if eta is not None:
            eta = min(eta, max(0, left) + (int(d.get("links_found") or 0) * ENRICH_SEC_PER_SITE if enrich_on else 0))
    d["eta_sec"] = round(eta) if eta is not None else None
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
        job = _job_dict(s.get_job(job_id))
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
        return _job_dict(s.get_job(job_id))
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
        job = _job_dict(s.get_job(job_id))
    finally:
        s.close()
    worker.wake.set()
    return job


@app.get("/api/jobs")
def list_jobs() -> list[dict[str, Any]]:
    s = Store()
    try:
        return [_job_dict(r) for r in s.list_jobs()]
    finally:
        s.close()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int) -> dict[str, Any]:
    s = Store()
    try:
        r = s.get_job(job_id)
        if not r:
            raise HTTPException(404, "no such job")
        d = _job_dict(r)
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
        return _job_dict(s.get_job(job_id))
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
        job = _job_dict(s.get_job(job_id))
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
