"""One-time admin bootstrap. Usage:
python scripts/create_admin.py admin@example.com "StrongPass123!"
Reads SUPABASE_PROJECT_URL + SUPABASE_SERVICE_ROLE_KEY from vercel-app/.env.deploy or env."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def load_env(p: Path) -> None:
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    load_env(ROOT / "vercel-app" / ".env.deploy")
    url = os.environ["SUPABASE_PROJECT_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    email, password = sys.argv[1], sys.argv[2]
    h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    r = httpx.post(f"{url}/auth/v1/admin/users", headers=h,
                   json={"email": email, "password": password, "email_confirm": True}, timeout=20)
    if r.status_code == 422 and "already" in r.text:
        r2 = httpx.get(f"{url}/auth/v1/admin/users?page=1&per_page=100", headers=h, timeout=20)
        uid = next(u["id"] for u in r2.json()["users"] if u["email"] == email)
    else:
        r.raise_for_status()
        uid = r.json()["id"]
    pr = httpx.post(f"{url}/rest/v1/profiles", headers={**h, "Prefer": "resolution=merge-duplicates"},
                    json={"user_id": uid, "role": "admin", "name": "Admin", "active": True},
                    params={"on_conflict": "user_id"}, timeout=20)
    pr.raise_for_status()
    print(f"admin ready: {email} ({uid})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
