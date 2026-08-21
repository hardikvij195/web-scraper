"""Admin-only endpoints: member management + credit adjustments."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from _auth import require_admin
from _db import gotrue, sb_get, sb_patch, sb_post

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])


class MemberIn(BaseModel):
    email: str
    password: str
    name: str | None = None


class MemberPatch(BaseModel):
    active: bool


class CreditsIn(BaseModel):
    user_id: str
    delta: int


@router.get("/members")
def members():
    profs = sb_get("profiles", {"select": "user_id,role,name,active,created_at", "order": "created_at.asc"})
    bals = {b["user_id"]: b["balance"] for b in sb_get("credit_balances", {"select": "user_id,balance"})}
    users = gotrue("admin/users?page=1&per_page=200", method="GET", admin=True).get("users", [])
    emails = {u["id"]: u.get("email") for u in users}
    return [{**p, "email": emails.get(p["user_id"], ""), "balance": bals.get(p["user_id"], 0)} for p in profs]


@router.post("/members")
def create_member(body: MemberIn):
    u = gotrue("admin/users", {"email": body.email, "password": body.password, "email_confirm": True},
               admin=True)
    sb_post("profiles", {"user_id": u["id"], "role": "member", "name": body.name, "active": True},
            params={"on_conflict": "user_id"}, prefer="resolution=merge-duplicates")
    return {"user_id": u["id"]}


@router.patch("/members/{user_id}")
def patch_member(user_id: str, body: MemberPatch):
    rows = sb_patch("profiles", {"active": body.active}, {"user_id": f"eq.{user_id}"})
    if not rows:
        raise HTTPException(404, "no such member")
    return {"ok": True}


@router.post("/credits")
def adjust_credits(body: CreditsIn):
    if body.delta == 0:
        raise HTTPException(400, "delta must be non-zero")
    sb_post("credits_ledger", {"user_id": body.user_id, "delta": body.delta, "reason": "admin_adjust"},
            prefer="return=minimal")
    bal = sb_get("credit_balances", {"user_id": f"eq.{body.user_id}", "select": "balance"})
    return {"balance": bal[0]["balance"] if bal else 0}
