"""Endpoints for the member's local agent (X-Agent-Token auth)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from _auth import User, agent_user
from _db import sb_get, sb_patch

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
