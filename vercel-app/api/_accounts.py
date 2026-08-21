"""Session endpoints: login/refresh/me/password. Settings + agent tokens live here too."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from _auth import User, current_user
from _db import gotrue, sb_get

router = APIRouter(prefix="/api")


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
