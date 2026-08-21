"""User-scoped leads listing for the cloud UI."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from _auth import User, current_user
from _db import sb_get

router = APIRouter(prefix="/api")


@router.get("/leads")
def leads(limit: int = 5000, user: User = Depends(current_user)):
    params = {"select": "*", "order": "scraped_at.desc", "limit": str(max(1, min(limit, 10000)))}
    if user.role != "admin":
        params["user_id"] = f"eq.{user.id}"
    rows = sb_get("web_scraper_leads", params)
    return {"total": len(rows), "rows": rows, "source": "supabase"}
