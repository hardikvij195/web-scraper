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
from _db import PACKS  # noqa: E402

app.include_router(accounts_router)
app.include_router(admin_router)


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


@app.get("/api/jobs")
def jobs():
    return []


@app.get("/api/leads")
def leads(unique: bool = True, limit: int = 5000, source: str = "supabase"):
    cfg = _supa()
    if not cfg:
        return {"total": 0, "rows": [], "source": "none"}
    url, key = cfg
    try:
        r = httpx.get(f"{url}/rest/v1/{TABLE}?select=*&order=scraped_at.desc&limit={max(1, min(limit, 10000))}",
                      headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=25)
        if r.status_code == 404:
            return {"total": 0, "rows": [], "source": "supabase"}
        r.raise_for_status()
        rows = r.json()
        return {"total": len(rows), "rows": rows, "source": "supabase"}
    except httpx.HTTPError as e:
        raise HTTPException(502, f"supabase: {e}") from e


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


@app.get("/api/suggest")
def suggest(q: str):
    q = (q or "").strip()
    if len(q) < 2:
        return {"source": "none", "keywords": []}
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_AI_STUDIO_API_KEY")
    prompt = (f"You help build Google Maps lead-generation searches. For the business type {q!r}, "
              "list 10 other Google Maps search keywords that find the same or closely related businesses. "
              "Reply as a JSON array of short strings only, no prose.")
    if key:
        try:
            r = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={key}",
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"temperature": 0.4, "maxOutputTokens": 1000,
                                           "responseMimeType": "application/json",
                                           "thinkingConfig": {"thinkingBudget": 0}}},
                timeout=25)
            r.raise_for_status()
            t = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            kws = [str(x).strip() for x in _json.loads(t[t.find("["): t.rfind("]") + 1]) if str(x).strip()][:12]
            if kws:
                return {"source": "gemini", "keywords": kws}
        except (httpx.HTTPError, ValueError, KeyError, IndexError):
            pass
    kws = []
    try:
        for seed in (q, f"{q} near", f"best {q}"):
            r = httpx.get("https://suggestqueries.google.com/complete/search",
                          params={"client": "firefox", "q": seed}, timeout=10,
                          headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                for s in r.json()[1]:
                    s = s.strip()
                    if s and s.lower() != q.lower() and s not in kws:
                        kws.append(s)
    except (httpx.HTTPError, ValueError, IndexError):
        pass
    return {"source": "autosuggest", "keywords": kws[:12]}


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


# job-creation / mutation endpoints are disabled in the cloud viewer
@app.post("/api/jobs")
@app.post("/api/supabase/sync")
def _disabled():
    raise HTTPException(400, "The scraper runs locally, not in the cloud. Run web-scraper on your PC; it syncs here.")
