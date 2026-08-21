"""PostgREST + GoTrue helpers. Service role key from env — server-side only."""
from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import HTTPException

SUPA_URL = (os.getenv("SUPABASE_PROJECT_URL") or "").strip().rstrip("/")
_SERVICE = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
_ANON = (os.getenv("SUPABASE_ANON_KEY") or "").strip()

PACKS = {
    "starter_3k": {"leads": 3000, "amount_inr": 88000, "label": "Starter — 3,000 leads", "usd": 10},
    "pro_5k": {"leads": 5000, "amount_inr": 132000, "label": "Pro — 5,000 leads", "usd": 15},
}


def _h(extra: dict | None = None) -> dict:
    h = {"apikey": _SERVICE, "Authorization": f"Bearer {_SERVICE}", "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def _req(method: str, path: str, *, params=None, json=None, prefer=None) -> Any:
    headers = _h({"Prefer": prefer} if prefer else None)
    try:
        r = httpx.request(method, f"{SUPA_URL}/rest/v1/{path}", params=params, json=json,
                          headers=headers, timeout=25)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"supabase: {e}") from e
    if r.status_code >= 300:
        raise HTTPException(502, f"supabase {r.status_code}: {r.text[:300]}")
    return r.json() if r.content and prefer != "return=minimal" else None


def sb_get(path: str, params: dict | None = None) -> Any:
    return _req("GET", path, params=params)


def sb_post(path: str, json: Any, params: dict | None = None, prefer: str = "return=representation") -> Any:
    return _req("POST", path, params=params, json=json, prefer=prefer)


def sb_patch(path: str, json: dict, params: dict) -> Any:
    return _req("PATCH", path, params=params, json=json, prefer="return=representation")


def sb_delete(path: str, params: dict) -> None:
    _req("DELETE", path, params=params, prefer="return=minimal")


def sb_rpc(fn: str, args: dict) -> Any:
    return _req("POST", f"rpc/{fn}", json=args)


def gotrue(path: str, json: dict | None = None, method: str = "POST",
           token: str | None = None, admin: bool = False) -> dict:
    """GoTrue REST. token = end-user JWT; admin=True uses the service role key."""
    key = _SERVICE if admin else _ANON
    headers = {"apikey": key, "Authorization": f"Bearer {token or key}", "Content-Type": "application/json"}
    try:
        r = httpx.request(method, f"{SUPA_URL}/auth/v1/{path}", json=json, headers=headers, timeout=20)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"auth: {e}") from e
    if r.status_code >= 400:
        try:
            j = r.json()
            detail = j.get("msg") or j.get("error_description") or j.get("message") or r.text[:200]
        except ValueError:
            detail = r.text[:200]
        raise HTTPException(401 if r.status_code in (400, 401, 403) else 502, detail)
    return r.json() if r.content else {}
