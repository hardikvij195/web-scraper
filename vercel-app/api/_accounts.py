"""Session endpoints: login/refresh/me/password. Settings + agent tokens live here too."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from _auth import User, current_user, hash_token, mask
from _db import gotrue, sb_get, sb_patch, sb_post

router = APIRouter(prefix="/api")


def new_secret() -> str:
    return secrets.token_urlsafe(32)


class LoginIn(BaseModel):
    email: str
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class PasswordIn(BaseModel):
    password: str


def _session(d: dict) -> dict:
    return {"access_token": d["access_token"], "refresh_token": d["refresh_token"],
            "expires_in": d.get("expires_in", 3600)}


@router.post("/login")
def login(body: LoginIn):
    d = gotrue("token?grant_type=password", {"email": body.email, "password": body.password})
    prof = sb_get("profiles", {"user_id": f"eq.{d['user']['id']}", "select": "role,active,name"})
    if not prof or not prof[0]["active"]:
        raise HTTPException(403, "account disabled or not provisioned")
    return {**_session(d), "email": body.email, "role": prof[0]["role"], "name": prof[0].get("name")}


@router.post("/refresh")
def refresh(body: RefreshIn):
    d = gotrue("token?grant_type=refresh_token", {"refresh_token": body.refresh_token})
    return _session(d)


@router.get("/me")
def me(user: User = Depends(current_user)):
    bal = sb_get("credit_balances", {"user_id": f"eq.{user.id}", "select": "balance"})
    return {"email": user.email, "role": user.role,
            "balance": bal[0]["balance"] if bal else 0}


@router.post("/me/password")
def change_password(body: PasswordIn, request: Request, user: User = Depends(current_user)):
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    gotrue("user", {"password": body.password}, method="PUT", token=token)
    return {"ok": True}


# ── settings ─────────────────────────────────────────────────────────────────
class SettingsIn(BaseModel):
    webhook_url: str | None = None
    gemini_key: str | None = None
    openai_key: str | None = None


def _settings_row(user_id: str) -> dict:
    rows = sb_get("user_settings", {"user_id": f"eq.{user_id}", "select": "*"})
    if rows:
        return rows[0]
    created = sb_post("user_settings", {"user_id": user_id, "webhook_secret": new_secret()})
    return created[0]


@router.get("/settings")
def get_settings(user: User = Depends(current_user)):
    s = _settings_row(user.id)
    return {"webhook_url": s.get("webhook_url"), "webhook_secret": s.get("webhook_secret"),
            "gemini_key_masked": mask(s.get("gemini_key")), "openai_key_masked": mask(s.get("openai_key"))}


@router.put("/settings")
def put_settings(body: SettingsIn, user: User = Depends(current_user)):
    _settings_row(user.id)
    patch = {k: (v or None) for k, v in body.model_dump().items() if v is not None}
    if patch:
        sb_patch("user_settings", patch, {"user_id": f"eq.{user.id}"})
    return {"ok": True}


@router.post("/settings/webhook-secret")
def regen_secret(user: User = Depends(current_user)):
    _settings_row(user.id)
    s = new_secret()
    sb_patch("user_settings", {"webhook_secret": s}, {"user_id": f"eq.{user.id}"})
    return {"webhook_secret": s}


# ── agent tokens ─────────────────────────────────────────────────────────────
class TokenIn(BaseModel):
    label: str | None = None


@router.get("/agent-tokens")
def list_tokens(user: User = Depends(current_user)):
    return sb_get("agent_tokens", {"user_id": f"eq.{user.id}",
                                   "select": "id,label,last_seen_at,revoked,created_at",
                                   "order": "created_at.desc"})


@router.post("/agent-tokens")
def create_token(body: TokenIn, user: User = Depends(current_user)):
    plain = "wsk_" + secrets.token_urlsafe(32)
    sb_post("agent_tokens", {"user_id": user.id, "token_hash": hash_token(plain), "label": body.label},
            prefer="return=minimal")
    return {"token": plain}


@router.post("/agent-tokens/{token_id}/revoke")
def revoke_token(token_id: int, user: User = Depends(current_user)):
    sb_patch("agent_tokens", {"revoked": True}, {"id": f"eq.{token_id}", "user_id": f"eq.{user.id}"})
    return {"ok": True}
