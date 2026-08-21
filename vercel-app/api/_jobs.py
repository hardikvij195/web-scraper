"""Browser-facing job queue CRUD."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from _auth import User, current_user
from _db import sb_get, sb_patch, sb_post

router = APIRouter(prefix="/api")


class JobIn(BaseModel):
    query: str = Field(min_length=2)
    location: str | None = None
    lat: float | None = None
    lng: float | None = None
    radius_km: float | None = None
    limit_places: int = Field(default=100, ge=1, le=5000)
    country: str | None = None


@router.post("/jobs")
def create_job(body: JobIn, user: User = Depends(current_user)):
    rows = sb_post("scrape_jobs", {**body.model_dump(), "user_id": user.id})
    return rows[0]


@router.get("/jobs")
def list_jobs(user: User = Depends(current_user)):
    params = {"select": "*", "order": "id.desc", "limit": "200"}
    if user.role != "admin":
        params["user_id"] = f"eq.{user.id}"
    return sb_get("scrape_jobs", params)


@router.get("/jobs/{job_id}")
def get_job(job_id: int, user: User = Depends(current_user)):
    params = {"id": f"eq.{job_id}", "select": "*"}
    if user.role != "admin":
        params["user_id"] = f"eq.{user.id}"
    rows = sb_get("scrape_jobs", params)
    if not rows:
        raise HTTPException(404, "no such job")
    return rows[0]


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, user: User = Depends(current_user)):
    params = {"id": f"eq.{job_id}"}
    if user.role != "admin":
        params["user_id"] = f"eq.{user.id}"
    rows = sb_patch("scrape_jobs", {"status": "cancelled"}, params)
    if not rows:
        raise HTTPException(404, "no such job")
    return {"ok": True}
