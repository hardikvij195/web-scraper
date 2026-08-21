"""Per-lead webhook delivery: HMAC-signed POSTs with retry + cron re-drive."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException

from _auth import User, current_user
from _db import sb_get, sb_patch, sb_post

router = APIRouter(prefix="/api")
MAX_ATTEMPTS = 8


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post_signed(url: str, secret: str, payload: dict) -> tuple[bool, str | None]:
    body = json.dumps(payload, ensure_ascii=False).encode()
    try:
        r = httpx.post(url, content=body, timeout=15,
                       headers={"Content-Type": "application/json", "X-Signature": sign(secret, body)})
        if 200 <= r.status_code < 300:
            return True, None
        return False, f"HTTP {r.status_code}"
    except httpx.HTTPError as e:
        return False, str(e)[:200]


def _settings(user_id: str) -> dict | None:
    rows = sb_get("user_settings", {"user_id": f"eq.{user_id}", "select": "webhook_url,webhook_secret"})
    return rows[0] if rows and rows[0].get("webhook_url") else None


def _lead_payload(user_id: str, place_key: str) -> dict | None:
    rows = sb_get("web_scraper_leads", {"user_id": f"eq.{user_id}", "place_key": f"eq.{place_key}",
                                        "select": "*"})
    if not rows:
        return None
    lead = rows[0]
    lead.pop("ai_summary", None)
    return {"event": "lead.verified", "lead": lead}


def _deliver_row(d: dict, s: dict) -> dict:
    """One delivery attempt; returns the updated row."""
    payload = _lead_payload(d["user_id"], d["lead_place_key"])
    ok, err = (False, "lead missing") if payload is None else _post_signed(d["url"], s["webhook_secret"], payload)
    attempts = d["attempts"] + 1
    now = datetime.now(timezone.utc)
    patch = {"attempts": attempts, "updated_at": now.isoformat()}
    if ok:
        patch.update({"status": "ok", "last_error": None, "next_retry_at": None})
    else:
        nxt = now + timedelta(minutes=min(2 ** attempts, 120))
        patch.update({"status": "failed" if attempts >= MAX_ATTEMPTS else "pending",
                      "last_error": err, "next_retry_at": nxt.isoformat()})
    rows = sb_patch("webhook_deliveries", patch, {"id": f"eq.{d['id']}"})
    return rows[0] if rows else {**d, **patch}


def enqueue_and_deliver(user_id: str, place_keys: list[str]) -> None:
    """Called from /api/agent/sync for freshly inserted verified leads."""
    s = _settings(user_id)
    if not s or not place_keys:
        return
    rows = sb_post("webhook_deliveries",
                   [{"user_id": user_id, "lead_place_key": k, "url": s["webhook_url"]} for k in place_keys])
    for d in rows:
        for _ in range(3):                 # 3 inline tries, then leave pending for cron
            d = _deliver_row(d, s)
            if d["status"] == "ok":
                break
            time.sleep(1)


@router.post("/webhooks/test")
def test_webhook(user: User = Depends(current_user)):
    s = _settings(user.id)
    if not s:
        raise HTTPException(400, "set a webhook URL first")
    ok, err = _post_signed(s["webhook_url"], s["webhook_secret"],
                           {"event": "test", "message": "web-scraper webhook test"})
    return {"ok": ok, "error": err}


@router.get("/webhooks/deliveries")
def deliveries(user: User = Depends(current_user)):
    return sb_get("webhook_deliveries", {"user_id": f"eq.{user.id}", "select": "*",
                                         "order": "id.desc", "limit": "50"})


@router.get("/cron/webhooks")
def cron_webhooks(authorization: str = Header(default="")):
    if not os.getenv("CRON_SECRET") or authorization != f"Bearer {os.getenv('CRON_SECRET')}":
        raise HTTPException(401, "bad cron secret")
    now = datetime.now(timezone.utc).isoformat()
    due = sb_get("webhook_deliveries", {"status": "eq.pending", "attempts": f"lt.{MAX_ATTEMPTS}",
                                        "next_retry_at": f"lte.{now}", "select": "*", "limit": "50"})
    redriven = 0
    for d in due:
        s = _settings(d["user_id"])
        if s:
            _deliver_row(d, s)
            redriven += 1
    return {"redriven": redriven}
