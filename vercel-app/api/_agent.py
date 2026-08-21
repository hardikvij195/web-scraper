"""Endpoints for the member's local agent (X-Agent-Token auth)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from _auth import User, agent_user
from _db import sb_get, sb_patch, sb_post, sb_rpc

router = APIRouter(prefix="/api/agent")
STALE_MIN = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def claim_is_stale(updated_at_iso: str, now: datetime) -> bool:
    ts = datetime.fromisoformat(updated_at_iso.replace("Z", "+00:00"))
    return (now - ts) > timedelta(minutes=STALE_MIN)


@router.get("/jobs")
def agent_jobs(user: User = Depends(agent_user)):
    rows = sb_get("scrape_jobs", {"user_id": f"eq.{user.id}",
                                  "status": "in.(queued,claimed,running)",
                                  "select": "*", "order": "id.asc"})
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        if r["status"] == "queued":
            out.append(r)
        elif r["claimed_by"] == user.agent_token_id or claim_is_stale(r["updated_at"], now):
            out.append(r)   # own in-flight job (resume) or stale claim from a dead agent
    return out


@router.post("/jobs/{job_id}/claim")
def claim(job_id: int, user: User = Depends(agent_user)):
    rows = sb_get("scrape_jobs", {"id": f"eq.{job_id}", "user_id": f"eq.{user.id}", "select": "*"})
    if not rows:
        raise HTTPException(404, "no such job")
    j = rows[0]
    now = datetime.now(timezone.utc)
    if j["status"] in ("claimed", "running") and j["claimed_by"] not in (None, user.agent_token_id) \
       and not claim_is_stale(j["updated_at"], now):
        raise HTTPException(409, "claimed by another agent")
    if j["status"] not in ("queued", "claimed", "running"):
        raise HTTPException(409, f"job is {j['status']}")
    upd = sb_patch("scrape_jobs", {"status": "claimed", "claimed_by": user.agent_token_id,
                                   "updated_at": _now_iso()}, {"id": f"eq.{job_id}"})
    return upd[0]


class ProgressIn(BaseModel):
    phase: str | None = None
    progress: dict = {}


@router.post("/jobs/{job_id}/progress")
def progress(job_id: int, body: ProgressIn, user: User = Depends(agent_user)):
    rows = sb_get("scrape_jobs", {"id": f"eq.{job_id}", "user_id": f"eq.{user.id}", "select": "status"})
    if not rows:
        raise HTTPException(404, "no such job")
    if rows[0]["status"] == "cancelled":
        return {"ok": True, "cancelled": True}
    sb_patch("scrape_jobs", {"status": "running", "phase": body.phase, "progress": body.progress,
                             "updated_at": _now_iso()}, {"id": f"eq.{job_id}"})
    return {"ok": True, "cancelled": False}


class DoneIn(BaseModel):
    status: str  # done | error
    error: str | None = None


@router.post("/jobs/{job_id}/done")
def done(job_id: int, body: DoneIn, user: User = Depends(agent_user)):
    if body.status not in ("done", "error"):
        raise HTTPException(400, "status must be done|error")
    cur = sb_get("scrape_jobs", {"id": f"eq.{job_id}", "user_id": f"eq.{user.id}", "select": "status"})
    if not cur:
        raise HTTPException(404, "no such job")
    if cur[0]["status"] == "paused_quota" and body.status == "done":
        return {"ok": True}   # sync already flagged quota exhaustion; keep that status
    sb_patch("scrape_jobs", {"status": body.status, "error": body.error, "updated_at": _now_iso()},
             {"id": f"eq.{job_id}"})
    return {"ok": True}


# ── sync ─────────────────────────────────────────────────────────────────────
FINISHED = {"done", "thin", "failed", "no_website"}

# Exactly the flattened columns the local agent sends (webscraper/supa.py _COLS) — PostgREST
# bulk upserts require every object to carry the same keys, and unknown keys 400 the batch.
LEAD_COLS = ["place_key", "name", "category", "phone", "whatsapp_number", "whatsapp_source",
             "email", "emails", "website", "instagram", "facebook", "linkedin", "twitter_x",
             "youtube", "tiktok", "address", "country", "rating", "reviews_count", "price_range",
             "lat", "lng", "summary", "owner", "team", "maps_url", "place_id", "enrich_status",
             "scraped_at", "job_id", "job_query", "job_location"]


def _norm(r: dict) -> dict:
    return {c: r.get(c) for c in LEAD_COLS}


def is_verified(row: dict) -> bool:
    if (row.get("enrich_status") or "pending") not in FINISHED:
        return False
    return bool((row.get("phone") or "").strip() or (row.get("email") or "").strip())


def partition_new(rows: list[dict], existing_keys: set[str]) -> tuple[list[dict], list[dict]]:
    new = [r for r in rows if r["place_key"] not in existing_keys]
    old = [r for r in rows if r["place_key"] in existing_keys]
    return new, old


class SyncIn(BaseModel):
    cloud_job_id: int
    rows: list[dict]


@router.post("/sync")
def sync(body: SyncIn, user: User = Depends(agent_user)):
    job = sb_get("scrape_jobs", {"id": f"eq.{body.cloud_job_id}", "user_id": f"eq.{user.id}",
                                 "select": "id,status"})
    if not job:
        raise HTTPException(404, "no such job")
    rows = [r for r in body.rows if r.get("place_key")]
    if not rows:
        return {"accepted": 0, "rejected_quota": 0, "debited": 0}
    keys = ",".join('"' + r["place_key"].replace('"', "") + '"' for r in rows)
    existing = {e["place_key"] for e in sb_get("web_scraper_leads",
                {"user_id": f"eq.{user.id}", "place_key": f"in.({keys})", "select": "place_key"})}
    new, old = partition_new(rows, existing)
    new_verified = [r for r in new if is_verified(r)]
    new_free = [r for r in new if not is_verified(r)]
    debited = sb_rpc("debit_credits", {"p_user": user.id, "p_requested": len(new_verified),
                                       "p_job": body.cloud_job_id}) or 0
    accepted_verified = new_verified[:debited]
    rejected = len(new_verified) - debited
    payload = []
    for r in accepted_verified:
        payload.append({**_norm(r), "user_id": user.id, "cloud_job_id": body.cloud_job_id, "verified": True})
    for r in new_free + old:
        payload.append({**_norm(r), "user_id": user.id, "cloud_job_id": body.cloud_job_id,
                        "verified": is_verified(r)})   # old rows refresh their flag; no re-debit
    try:
        for i in range(0, len(payload), 200):
            sb_post("web_scraper_leads", payload[i:i + 200],
                    params={"on_conflict": "user_id,place_key"},
                    prefer="resolution=merge-duplicates,return=minimal")
    except HTTPException:
        if debited:   # refund — the debited leads never landed
            sb_post("credits_ledger", {"user_id": user.id, "delta": debited,
                                       "reason": "admin_adjust", "job_id": body.cloud_job_id},
                    prefer="return=minimal")
        raise
    if rejected:
        sb_patch("scrape_jobs", {"status": "paused_quota", "updated_at": _now_iso()},
                 {"id": f"eq.{body.cloud_job_id}"})
    elif job[0]["status"] == "paused_quota":
        # fully synced after a top-up — unblock so /done can complete the job
        sb_patch("scrape_jobs", {"status": "running", "updated_at": _now_iso()},
                 {"id": f"eq.{body.cloud_job_id}"})
    try:
        from _webhooks import enqueue_and_deliver
        enqueue_and_deliver(user.id, [r["place_key"] for r in accepted_verified])
    except ImportError:
        pass   # _webhooks lands in the next task
    return {"accepted": len(payload), "rejected_quota": rejected, "debited": debited}
