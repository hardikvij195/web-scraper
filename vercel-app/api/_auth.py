"""Auth dependencies: browser JWT sessions and local-agent tokens."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException

from _db import gotrue, sb_get, sb_patch


@dataclass
class User:
    id: str
    email: str
    role: str
    agent_token_id: int | None = field(default=None)


def mask(s: str | None) -> str | None:
    if not s:
        return None
    return "…" + s[-4:]


def hash_token(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def _profile(user_id: str) -> dict:
    rows = sb_get("profiles", {"user_id": f"eq.{user_id}", "select": "role,active,name"})
    if not rows or not rows[0]["active"]:
        raise HTTPException(403, "account disabled or not provisioned")
    return rows[0]


def current_user(authorization: str = Header(default="")) -> User:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    u = gotrue("user", method="GET", token=token)          # 401s on bad/expired token
    prof = _profile(u["id"])
    return User(id=u["id"], email=u.get("email") or "", role=prof["role"])


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "admin only")
    return user


def agent_user(x_agent_token: str = Header(default="")) -> User:
    if not x_agent_token:
        raise HTTPException(401, "missing X-Agent-Token")
    rows = sb_get("agent_tokens", {"token_hash": f"eq.{hash_token(x_agent_token)}",
                                   "revoked": "eq.false", "select": "id,user_id"})
    if not rows:
        raise HTTPException(401, "invalid agent token")
    tok = rows[0]
    prof = _profile(tok["user_id"])
    sb_patch("agent_tokens", {"last_seen_at": datetime.now(timezone.utc).isoformat()},
             {"id": f"eq.{tok['id']}"})
    return User(id=tok["user_id"], email=prof.get("name") or "", role=prof["role"],
                agent_token_id=tok["id"])
