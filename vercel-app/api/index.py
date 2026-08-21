"""Cloud viewer for web-scraper — read-only serverless API on Vercel.

The scraper engine (Playwright + long-running worker + SQLite) CANNOT run on Vercel, so this
deploys only the dashboard: it reads the shared leads from Supabase (which each user's LOCAL
scraper pushes to), plus AI keyword suggestions and place geocoding. Job creation is disabled.
"""
from __future__ import annotations

import json as _json
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="web-scraper cloud viewer")
TABLE = "web_scraper_leads"
HTML = (Path(__file__).parent.parent / "index.html")

import sys as _sys

if str(Path(__file__).parent) not in _sys.path:   # sibling imports under `api.index:app` too
    _sys.path.insert(0, str(Path(__file__).parent))

from _accounts import router as accounts_router  # noqa: E402
from _admin import router as admin_router  # noqa: E402
from _agent import router as agent_router  # noqa: E402
from _ai import router as ai_router  # noqa: E402
from _db import PACKS  # noqa: E402
from _jobs import router as jobs_router  # noqa: E402
from _leads import router as leads_router  # noqa: E402
from _pay import router as pay_router  # noqa: E402
from _webhooks import router as webhooks_router  # noqa: E402

app.include_router(accounts_router)
app.include_router(admin_router)
app.include_router(agent_router)
app.include_router(ai_router)
app.include_router(jobs_router)
app.include_router(leads_router)
app.include_router(pay_router)
app.include_router(webhooks_router)


@app.get("/api/config")
def config():
    return {"cloud": True, "packs": [{"id": k, **v} for k, v in PACKS.items()]}


def _supa() -> tuple[str, str] | None:
    url = (os.getenv("SUPABASE_PROJECT_URL") or "").strip().rstrip("/")
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY") or "").strip()
    return (url, key) if url and key else None


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return HTML.read_text(encoding="utf-8")


@app.get("/api/health")
def health():
    return {"ok": True, "cloud": True, "worker_alive": False, "current_job": None,
            "supabase": _supa() is not None}


@app.get("/api/supabase/status")
def supa_status():
    cfg = _supa()
    if not cfg:
        return {"configured": False, "reachable": False, "table_exists": False, "count": 0, "cloud": True}
    url, key = cfg
    try:
        r = httpx.get(f"{url}/rest/v1/{TABLE}?select=place_key",
                      headers={"apikey": key, "Authorization": f"Bearer {key}", "Prefer": "count=exact", "Range": "0-0"},
                      timeout=15)
        exists = r.status_code != 404
        cr = r.headers.get("content-range", "*/0")
        count = int(cr.split("/")[-1]) if "/" in cr and cr.split("/")[-1].isdigit() else 0
        return {"configured": True, "reachable": True, "table_exists": exists, "count": count, "cloud": True}
    except httpx.HTTPError:
        return {"configured": True, "reachable": False, "table_exists": False, "count": 0, "cloud": True}


@app.get("/api/geocode")
def geocode(q: str):
    q = (q or "").strip()
    if len(q) < 2:
        return []
    try:
        r = httpx.get("https://nominatim.openstreetmap.org/search",
                      params={"q": q, "format": "jsonv2", "limit": 6, "addressdetails": 1},
                      headers={"User-Agent": "web-scraper-cloud/1.0", "Accept-Language": "en"}, timeout=12)
        r.raise_for_status()
    except httpx.HTTPError:
        return []
    out = []
    for it in r.json():
        a = it.get("address") or {}
        short = ", ".join(x for x in (a.get("suburb") or a.get("neighbourhood") or a.get("town") or a.get("village"),
                                      a.get("city") or a.get("county"), a.get("state"), a.get("country")) if x)
        out.append({"name": it.get("display_name"), "short": short or it.get("display_name"),
                    "lat": float(it["lat"]), "lng": float(it["lon"]),
                    "bbox": [float(x) for x in it.get("boundingbox", [])] or None})
    return out


