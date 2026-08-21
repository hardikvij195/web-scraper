# Lead Finder Cloud Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn web-scraper into a multi-user product: Supabase auth (admin/member), cloud job queue executed by each member's local agent, credit packs sold via Razorpay + PayU, per-lead webhooks, and member-owned AI keys.

**Architecture:** Vercel FastAPI (`vercel-app/api/`) + Supabase (auth, Postgres, RLS) is the control plane: login, jobs queue, leads, credits, payments, webhook dispatch. The member's PC runs `python -m webscraper agent`, which mirrors cloud jobs into the existing local pipeline (Playwright scrape → httpx enrich) and syncs results up. Verified leads (enrichment finished AND phone-or-email) debit credits and fire the member's webhook.

**Tech Stack:** Python 3.13, FastAPI, httpx, Supabase (PostgREST + GoTrue REST, no SDK), vanilla-JS single page, Razorpay checkout.js, PayU form-post, pytest.

**Spec:** `docs/superpowers/specs/2026-08-21-lead-finder-cloud-design.md`

## Global Constraints

- Server-side Supabase access uses PostgREST/GoTrue REST over httpx with `SUPABASE_SERVICE_ROLE_KEY` from env — never ship that key to the client, never commit it (`.env.deploy` is gitignored; the incident on 2026-08-21 is why).
- Client auth uses only `SUPABASE_ANON_KEY` indirectly — the browser never talks to Supabase; it talks to our `/api/*` endpoints.
- Pack prices are server-side constants: `starter_3k` = 3,000 leads = ₹880.00 (88000 paise), `pro_5k` = 5,000 leads = ₹1,320.00 (132000 paise). Client-sent amounts are never trusted.
- Verified lead = `enrich_status != 'pending'` AND (`phone` non-empty OR `email` non-empty). Only verified leads debit credits and fire webhooks.
- Dev-server verification is **curl only** (user directive: no Playwright against localhost). The user checks UI in their own browser.
- New Python deps allowed: `python-multipart` (PayU return form) only. Everything else uses existing fastapi/httpx/pytest.
- Cloud UI edits go to `vercel-app/index.html` only; `webscraper/static/index.html` (local UI) stays untouched except where a task says otherwise.
- Commit after every task with a real message (no `1`/`2`/`3` messages).
- All new cloud tables RLS-enabled; policies per spec (owner-or-admin). Service role bypasses RLS for the API.
- Supabase project: the one already in `vercel-app/.env.deploy` (`SUPABASE_PROJECT_URL`, ref `gfgkcnjxvxlusplwmvae`).

## File Structure (end state)

```
supabase_migrations/
  001_accounts.sql          profiles, user_settings, agent_tokens, is_admin(), RLS
  002_jobs_credits.sql      scrape_jobs, orders, credits_ledger, webhook_deliveries, debit_credits RPC
  003_leads_multiuser.sql   web_scraper_leads: user_id/cloud_job_id/verified/ai_summary/webhook_status, new PK, backfill, RLS
scripts/
  apply_migrations.py       psycopg2 runner (pooler-region scan) for the 3 files
  create_admin.py           bootstrap the admin user via GoTrue admin REST
vercel-app/api/
  index.py                  app assembly: mounts routers, serves index.html, /api/config, /api/health
  _db.py                    PostgREST helpers (sb_get/sb_post/sb_patch/sb_delete/sb_rpc) + PACKS
  _auth.py                  session auth dep (JWT→GoTrue /user→profile), agent-token dep, mask()
  _accounts.py              /api/login /api/refresh /api/me /api/me/password /api/settings /api/agent-tokens
  _admin.py                 /api/admin/members (list/create/patch) /api/admin/credits
  _jobs.py                  browser job CRUD: /api/jobs (POST/GET/list) /api/jobs/{id}/cancel
  _agent.py                 /api/agent/jobs /claim /progress /done /api/agent/sync (verify+debit+upsert)
  _webhooks.py              HMAC sign, deliver with retries, /api/webhooks/test, /api/cron/webhooks, deliveries list
  _pay.py                   Razorpay order/verify/webhook + PayU initiate/return
  _ai.py                    /api/suggest (member keys), /api/leads/summarize
  _leads.py                 /api/leads (user-scoped, admin sees all)
vercel-app/index.html       login overlay + tabs Jobs/Leads/Settings/Billing/Admin
webscraper/agent.py         cloud poll loop wrapping the existing Worker pipeline
webscraper/cli.py           new `agent` command
webscraper/store.py         jobs.cloud_id column in _migrate()
tests/cloud/                pytest for pure logic (conftest puts vercel-app/api on sys.path)
```

---

### Task 1: Supabase migrations + apply script

**Files:**
- Create: `supabase_migrations/001_accounts.sql`
- Create: `supabase_migrations/002_jobs_credits.sql`
- Create: `supabase_migrations/003_leads_multiuser.sql`
- Create: `scripts/apply_migrations.py`

**Interfaces:**
- Produces: tables `profiles(user_id, role, name, active, created_at)`, `user_settings(user_id, webhook_url, webhook_secret, gemini_key, openai_key, updated_at)`, `agent_tokens(id, user_id, token_hash, label, last_seen_at, revoked, created_at)`, `scrape_jobs(id, user_id, query, location, lat, lng, radius_km, limit_places, country, status, phase, progress, claimed_by, error, created_at, updated_at)`, `orders`, `credits_ledger`, `webhook_deliveries`; SQL functions `is_admin()`, `debit_credits(p_user uuid, p_requested int, p_job bigint) returns int`, view `credit_balances`.
- Consumes: nothing (first task).

- [ ] **Step 1: Write `001_accounts.sql`**

```sql
-- 001_accounts.sql — idempotent
create table if not exists public.profiles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  role text not null check (role in ('admin','member')),
  name text,
  active boolean not null default true,
  created_at timestamptz not null default now()
);

create table if not exists public.user_settings (
  user_id uuid primary key references auth.users(id) on delete cascade,
  webhook_url text,
  webhook_secret text,
  gemini_key text,
  openai_key text,
  updated_at timestamptz not null default now()
);

create table if not exists public.agent_tokens (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  token_hash text not null unique,
  label text,
  last_seen_at timestamptz,
  revoked boolean not null default false,
  created_at timestamptz not null default now()
);

-- security definer so RLS policies can call it without recursing into profiles' own policies
create or replace function public.is_admin() returns boolean
language sql stable security definer set search_path = public as
$$ select exists(select 1 from profiles p where p.user_id = auth.uid() and p.role = 'admin' and p.active) $$;

alter table public.profiles enable row level security;
alter table public.user_settings enable row level security;
alter table public.agent_tokens enable row level security;

drop policy if exists "own or admin read" on public.profiles;
create policy "own or admin read" on public.profiles for select
  using (user_id = auth.uid() or public.is_admin());

drop policy if exists "owner all" on public.user_settings;
create policy "owner all" on public.user_settings for all
  using (user_id = auth.uid()) with check (user_id = auth.uid());

drop policy if exists "own or admin read" on public.agent_tokens;
create policy "own or admin read" on public.agent_tokens for select
  using (user_id = auth.uid() or public.is_admin());
-- writes to all three go through the service-role API only (service role bypasses RLS)
```

- [ ] **Step 2: Write `002_jobs_credits.sql`**

```sql
-- 002_jobs_credits.sql — idempotent
create table if not exists public.scrape_jobs (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  query text not null,
  location text,
  lat numeric, lng numeric, radius_km numeric,
  limit_places int not null default 100,
  country text,
  status text not null default 'queued'
    check (status in ('queued','claimed','running','paused_quota','done','error','cancelled')),
  phase text,
  progress jsonb not null default '{}'::jsonb,
  claimed_by bigint references public.agent_tokens(id),
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_scrape_jobs_user on public.scrape_jobs(user_id, status);

create table if not exists public.orders (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  pack text not null check (pack in ('starter_3k','pro_5k')),
  leads int not null,
  amount_inr int not null,             -- paise
  gateway text not null check (gateway in ('razorpay','payu')),
  gateway_order_id text unique,        -- razorpay order_id / payu txnid
  status text not null default 'created' check (status in ('created','paid','failed')),
  raw jsonb,
  created_at timestamptz not null default now(),
  paid_at timestamptz
);

create table if not exists public.credits_ledger (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  delta int not null,
  reason text not null check (reason in ('purchase','debit','admin_adjust')),
  order_id bigint references public.orders(id),
  job_id bigint references public.scrape_jobs(id),
  created_at timestamptz not null default now()
);
create index if not exists idx_ledger_user on public.credits_ledger(user_id);

create or replace view public.credit_balances as
  select user_id, coalesce(sum(delta), 0)::int as balance
  from public.credits_ledger group by user_id;

create table if not exists public.webhook_deliveries (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  lead_place_key text not null,
  url text not null,
  status text not null default 'pending' check (status in ('pending','ok','failed')),
  attempts int not null default 0,
  last_error text,
  next_retry_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_deliveries_pending on public.webhook_deliveries(status, next_retry_at);

-- Debits min(balance, requested); returns how many were actually debited. Race-safe via
-- per-user advisory lock. Called with service role only.
create or replace function public.debit_credits(p_user uuid, p_requested int, p_job bigint)
returns int language plpgsql security definer set search_path = public as $$
declare v_balance int; v_debit int;
begin
  if p_requested <= 0 then return 0; end if;
  perform pg_advisory_xact_lock(hashtext(p_user::text));
  select coalesce(sum(delta), 0) into v_balance from credits_ledger where user_id = p_user;
  v_debit := least(v_balance, p_requested);
  if v_debit > 0 then
    insert into credits_ledger(user_id, delta, reason, job_id) values (p_user, -v_debit, 'debit', p_job);
  end if;
  return v_debit;
end $$;

alter table public.scrape_jobs enable row level security;
alter table public.orders enable row level security;
alter table public.credits_ledger enable row level security;
alter table public.webhook_deliveries enable row level security;

drop policy if exists "own or admin read" on public.scrape_jobs;
create policy "own or admin read" on public.scrape_jobs for select
  using (user_id = auth.uid() or public.is_admin());
drop policy if exists "own or admin read" on public.orders;
create policy "own or admin read" on public.orders for select
  using (user_id = auth.uid() or public.is_admin());
drop policy if exists "own or admin read" on public.credits_ledger;
create policy "own or admin read" on public.credits_ledger for select
  using (user_id = auth.uid() or public.is_admin());
drop policy if exists "own or admin read" on public.webhook_deliveries;
create policy "own or admin read" on public.webhook_deliveries for select
  using (user_id = auth.uid() or public.is_admin());
```

- [ ] **Step 3: Write `003_leads_multiuser.sql`**

```sql
-- 003_leads_multiuser.sql — idempotent. Run AFTER scripts/create_admin.py has created the admin
-- (the backfill needs one profiles row with role='admin').
alter table public.web_scraper_leads add column if not exists user_id uuid references auth.users(id);
alter table public.web_scraper_leads add column if not exists cloud_job_id bigint;
alter table public.web_scraper_leads add column if not exists verified boolean not null default false;
alter table public.web_scraper_leads add column if not exists ai_summary text;
alter table public.web_scraper_leads add column if not exists webhook_status text;

update public.web_scraper_leads
  set user_id = (select user_id from public.profiles where role = 'admin' order by created_at limit 1)
  where user_id is null;
alter table public.web_scraper_leads alter column user_id set not null;

-- PK place_key → (user_id, place_key) so two members can own the same place
do $$ begin
  if exists (select 1 from information_schema.table_constraints
             where table_name = 'web_scraper_leads' and constraint_name = 'web_scraper_leads_pkey'
               and constraint_type = 'PRIMARY KEY')
     and not exists (select 1 from information_schema.constraint_column_usage
                     where constraint_name = 'web_scraper_leads_pkey' and column_name = 'user_id') then
    alter table public.web_scraper_leads drop constraint web_scraper_leads_pkey;
    alter table public.web_scraper_leads add primary key (user_id, place_key);
  end if;
end $$;

drop policy if exists "service role full access" on public.web_scraper_leads;
drop policy if exists "own or admin read" on public.web_scraper_leads;
create policy "own or admin read" on public.web_scraper_leads for select
  using (user_id = auth.uid() or public.is_admin());
```

- [ ] **Step 4: Write `scripts/apply_migrations.py`**

Reuses the pooler-region-scan trick from `webscraper/supa.py:create_table_via_pg`. Reads `SUPABASE_PROJECT_URL` + `SUPABASE_DB_PASS` from env (load `.env` + `vercel-app/.env.deploy` via manual parse — no new dep):

```python
"""Apply supabase_migrations/*.sql in order over direct Postgres.
Usage: python scripts/apply_migrations.py [001 002 003]
Needs SUPABASE_PROJECT_URL and SUPABASE_DB_PASS in env or .env / vercel-app/.env.deploy.
Fallback if no DB pass: paste each file into the Supabase SQL editor by hand.
"""
from __future__ import annotations
import os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def load_env(p: Path) -> None:
    if not p.exists(): return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

def main() -> int:
    load_env(ROOT / ".env"); load_env(ROOT / "vercel-app" / ".env.deploy")
    url = (os.getenv("SUPABASE_PROJECT_URL") or "").strip().rstrip("/")
    pw = (os.getenv("SUPABASE_DB_PASS") or "").strip()
    if not url or not pw:
        print("SUPABASE_PROJECT_URL / SUPABASE_DB_PASS missing — paste the SQL into the Supabase SQL editor instead.")
        return 1
    ref = url.split("//")[-1].split(".")[0]
    import psycopg2
    wanted = sys.argv[1:] or ["001", "002", "003"]
    files = sorted(f for f in (ROOT / "supabase_migrations").glob("*.sql")
                   if any(f.name.startswith(w) for w in wanted))
    regions = ["ap-south-1", "ap-southeast-1", "ap-northeast-1", "us-east-1", "us-east-2",
               "us-west-1", "us-west-2", "eu-west-1", "eu-west-2", "eu-central-1",
               "ap-southeast-2", "ap-northeast-2", "sa-east-1", "ca-central-1"]
    for reg in regions:
        try:
            c = psycopg2.connect(host=f"aws-0-{reg}.pooler.supabase.com", port=5432,
                                 user=f"postgres.{ref}", password=pw, dbname="postgres",
                                 connect_timeout=6, sslmode="require")
        except Exception:
            continue
        try:
            c.autocommit = True
            cur = c.cursor()
            for f in files:
                print(f"applying {f.name} ...")
                cur.execute(f.read_text(encoding="utf-8"))
            print("done")
            return 0
        finally:
            c.close()
    print("could not reach the Postgres pooler in any region")
    return 1

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Apply migrations 001 + 002 (003 waits for Task 4's admin bootstrap)**

Run: `python scripts/apply_migrations.py 001 002`
Expected: `applying 001_accounts.sql ... applying 002_jobs_credits.sql ... done`
If it exits 1 for missing `SUPABASE_DB_PASS`: **checkpoint — ask the user** to either add `SUPABASE_DB_PASS` to `.env` or paste `001` + `002` into the Supabase SQL editor, then continue.

- [ ] **Step 6: Verify tables exist over PostgREST**

Run (PowerShell, values from `vercel-app/.env.deploy`):
```
$u = "https://gfgkcnjxvxlusplwmvae.supabase.co"; $k = "<service role key from vercel-app/.env.deploy>"
curl.exe -s -o NUL -w "%{http_code}`n" "$u/rest/v1/profiles?select=user_id&limit=1" -H "apikey: $k" -H "Authorization: Bearer $k"
curl.exe -s -o NUL -w "%{http_code}`n" "$u/rest/v1/scrape_jobs?select=id&limit=1" -H "apikey: $k" -H "Authorization: Bearer $k"
```
Expected: `200` twice (404 = migration didn't apply).

- [ ] **Step 7: Commit**

```bash
git add supabase_migrations scripts/apply_migrations.py
git commit -m "feat: Supabase schema for accounts, jobs queue, credits, webhooks"
```

---

### Task 2: Cloud API core — `_db.py`, `_auth.py`, login endpoints

**Files:**
- Create: `vercel-app/api/_db.py`
- Create: `vercel-app/api/_auth.py`
- Create: `vercel-app/api/_accounts.py` (login/refresh/me only in this task; settings endpoints come in Task 5)
- Modify: `vercel-app/api/index.py` (mount router, add `/api/config`, keep existing viewer endpoints working)
- Create: `tests/cloud/conftest.py`, `tests/cloud/test_auth.py`

**Interfaces:**
- Produces:
  - `_db.py`: `SUPA_URL: str`, `sb_get(path: str, params: dict | None = None) -> list|dict`, `sb_post(path, json, params=None, prefer="return=representation") -> list|dict`, `sb_patch(path, json, params) -> list`, `sb_delete(path, params) -> None`, `sb_rpc(fn: str, args: dict) -> Any`, `PACKS: dict` (`{"starter_3k": {"leads": 3000, "amount_inr": 88000}, "pro_5k": {"leads": 5000, "amount_inr": 132000}}`), `gotrue(path, json=None, method="POST", token=None, admin=False) -> dict`.
  - `_auth.py`: `@dataclass User: id: str; email: str; role: str; agent_token_id: int | None`, dependency `current_user(authorization: str = Header(...)) -> User`, `require_admin(user: User = Depends(current_user)) -> User`, `agent_user(x_agent_token: str = Header(...)) -> User` (returns the token owner, updates `last_seen_at`, sets `user.agent_token_id`), `mask(s: str | None) -> str | None` (`"…" + last 4`, None-safe), `hash_token(t: str) -> str` (sha256 hex).
  - `_accounts.py`: `router: APIRouter` with `POST /api/login {email, password} -> {access_token, refresh_token, expires_in, role, email}`, `POST /api/refresh {refresh_token} -> same`, `GET /api/me -> {email, role, balance}`, `POST /api/me/password {password}`.
  - `index.py`: `GET /api/config -> {cloud: true, packs: PACKS-with-labels}` (no secrets).
- Consumes: Task 1 tables via PostgREST.

- [ ] **Step 1: Write failing tests**

`tests/cloud/conftest.py`:
```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "vercel-app" / "api"))
```

`tests/cloud/test_auth.py`:
```python
import hashlib
from _auth import mask, hash_token

def test_mask_none_and_short():
    assert mask(None) is None
    assert mask("") is None
    assert mask("abc") == "…abc"

def test_mask_long_shows_last4():
    assert mask("sk-1234567890") == "…7890"

def test_hash_token_is_sha256_hex():
    assert hash_token("tok") == hashlib.sha256(b"tok").hexdigest()
```

- [ ] **Step 2: Run tests, verify failure**

Run: `python -m pytest tests/cloud/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named '_auth'`

- [ ] **Step 3: Implement `_db.py`**

```python
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
    if extra: h.update(extra)
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
        detail = (r.json().get("msg") or r.json().get("error_description") or r.text[:200]) if r.content else r.text[:200]
        raise HTTPException(401 if r.status_code in (400, 401, 403) else 502, detail)
    return r.json() if r.content else {}
```

- [ ] **Step 4: Implement `_auth.py`**

```python
"""Auth dependencies: browser JWT sessions and local-agent tokens."""
from __future__ import annotations
import hashlib
from dataclasses import dataclass, field
from fastapi import Depends, Header, HTTPException
from _db import gotrue, sb_get, sb_patch

@dataclass
class User:
    id: str
    email: str
    role: str
    agent_token_id: int | None = field(default=None)

def mask(s: str | None) -> str | None:
    if not s: return None
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
    sb_patch("agent_tokens", {"last_seen_at": "now()"}, {"id": f"eq.{tok['id']}"})
    return User(id=tok["user_id"], email=prof.get("name") or "", role=prof["role"],
                agent_token_id=tok["id"])
```

- [ ] **Step 5: Implement `_accounts.py` (login part)**

```python
"""Session endpoints: login/refresh/me/password. Settings + agent tokens added in Task 5."""
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
```

- [ ] **Step 6: Wire into `index.py`**

In `vercel-app/api/index.py`: keep existing endpoints (`/`, `/api/health`, `/api/geocode`, supabase status) but add after `app = FastAPI(...)`:

```python
from _accounts import router as accounts_router
from _db import PACKS
app.include_router(accounts_router)

@app.get("/api/config")
def config():
    return {"cloud": True, "packs": [{"id": k, **v} for k, v in PACKS.items()]}
```

Leave the old `GET /api/jobs` stub + `_disabled` handlers for now — Task 6 removes them.

- [ ] **Step 7: Run tests, verify pass**

Run: `python -m pytest tests/cloud -v`
Expected: 3 PASS. Also `python -c "import sys; sys.path.insert(0,'vercel-app/api'); import index"` → no import errors.

- [ ] **Step 8: Smoke test locally**

Run: `cd vercel-app && python -m uvicorn api.index:app --port 8899` (background), then:
`curl -s http://127.0.0.1:8899/api/config` → JSON with 2 packs; `curl -s -X POST http://127.0.0.1:8899/api/login -H "Content-Type: application/json" -d "{\"email\":\"x@x.com\",\"password\":\"wrong\"}"` → 401 JSON. Kill server.
(Env: load `SUPABASE_PROJECT_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_ANON_KEY` from `vercel-app/.env.deploy` — **checkpoint: `SUPABASE_ANON_KEY` is not on disk yet; ask the user to fetch it from Supabase dashboard → Settings → API and add it to `vercel-app/.env.deploy`.**)

- [ ] **Step 9: Commit**

```bash
git add vercel-app/api tests/cloud
git commit -m "feat: cloud API auth core — PostgREST helpers, JWT sessions, login/refresh/me"
```

---

### Task 3: Frontend login + session plumbing

**Files:**
- Modify: `vercel-app/index.html`

**Interfaces:**
- Consumes: `POST /api/login`, `POST /api/refresh`, `GET /api/me`, `GET /api/config`.
- Produces: JS globals used by later tasks: `SESSION` (object or null), `authApi(path, opt)` (fetch wrapper adding `Authorization`, auto-refresh-once on 401, throws after), `logout()`, `IS_ADMIN()` (fn), and a `#login` overlay.

- [ ] **Step 1: Add login overlay HTML**

In `vercel-app/index.html`, directly after `<body>` insert:

```html
<div id="login" style="position:fixed;inset:0;z-index:1000;display:none;align-items:center;justify-content:center;background:var(--bg,#0b1620)">
  <form id="login-form" class="card" style="width:340px;display:flex;flex-direction:column;gap:10px">
    <h2 style="margin:0">Lead Finder — sign in</h2>
    <input id="login-email" type="email" placeholder="email" required autocomplete="username">
    <input id="login-pass" type="password" placeholder="password" required autocomplete="current-password">
    <button class="btn" type="submit">Sign in</button>
    <div id="login-err" style="color:#ff7a7a;font-size:12.5px"></div>
    <div class="muted" style="font-size:12px">No account? Ask your admin to create one.</div>
  </form>
</div>
```

- [ ] **Step 2: Add session JS**

At the TOP of the main `<script>` block (right after `const $ = s => document.querySelector(s);`):

```javascript
// ── auth session ─────────────────────────────────────────────────────────────
let SESSION = null; try { SESSION = JSON.parse(localStorage.getItem('ws_session')||'null'); } catch {}
window.IS_ADMIN = () => !!(SESSION && SESSION.role === 'admin');
function saveSession(s) { SESSION = s; if (s) localStorage.setItem('ws_session', JSON.stringify(s)); else localStorage.removeItem('ws_session'); }
function showLogin(show) { $('#login').style.display = show ? 'flex' : 'none'; }
window.logout = () => { saveSession(null); location.reload(); };

async function rawApi(path, opt) {
  const headers = Object.assign({'Content-Type':'application/json'},
    SESSION ? {'Authorization': 'Bearer ' + SESSION.access_token} : {}, (opt||{}).headers||{});
  return fetch(path, Object.assign({}, opt||{}, {headers}));
}
async function authApi(path, opt) {
  let r = await rawApi(path, opt);
  if (r.status === 401 && SESSION && SESSION.refresh_token) {
    const rr = await fetch('/api/refresh', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({refresh_token: SESSION.refresh_token})});
    if (rr.ok) { const s = await rr.json(); saveSession(Object.assign({}, SESSION, s)); r = await rawApi(path, opt); }
  }
  if (r.status === 401) { saveSession(null); showLogin(true); throw new Error('signed out'); }
  if (!r.ok) throw new Error((await r.json().catch(()=>({detail:r.statusText}))).detail || r.statusText);
  return r.json();
}
document.getElementById('login-form').addEventListener('submit', async e => {
  e.preventDefault(); $('#login-err').textContent = '';
  try {
    const s = await (await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({email: $('#login-email').value, password: $('#login-pass').value})})).json();
    if (!s.access_token) throw new Error(s.detail || 'login failed');
    saveSession(s); showLogin(false); location.reload();
  } catch (err) { $('#login-err').textContent = err.message; }
});
if (!SESSION) showLogin(true);
```

Then change the existing `const api = async (path, opt) => {...}` helper to delegate: `const api = (path, opt) => authApi(path, opt);` (single line, keeps every existing call-site working and authenticated).

- [ ] **Step 3: Add a signed-in header chip**

After the `<nav class="tabs">` element:

```html
<div id="whoami" class="muted" style="margin-left:auto;font-size:12.5px;display:flex;gap:10px;align-items:center">
  <span id="whoami-email"></span><span id="whoami-credits"></span>
  <button class="tab" onclick="logout()">Sign out</button>
</div>
```

And in the boot path (where `refresh()` is first called), when a session exists:
```javascript
authApi('/api/me').then(m => {
  $('#whoami-email').textContent = m.email + (m.role === 'admin' ? ' (admin)' : '');
  $('#whoami-credits').textContent = '· ' + m.balance + ' credits';
}).catch(()=>{});
```

- [ ] **Step 4: Verify with curl + user check**

`cd vercel-app && python -m uvicorn api.index:app --port 8899` → `curl -s http://127.0.0.1:8899/ | grep -c "login-form"` → `1`. Kill server. User checks visuals in their own browser after next deploy.

- [ ] **Step 5: Commit**

```bash
git add vercel-app/index.html
git commit -m "feat: login overlay + authenticated fetch wrapper in cloud UI"
```

---

### Task 4: Admin bootstrap + admin API + Admin tab

**Files:**
- Create: `scripts/create_admin.py`
- Create: `vercel-app/api/_admin.py`
- Modify: `vercel-app/api/index.py` (mount router)
- Modify: `vercel-app/index.html` (Admin tab)

**Interfaces:**
- Consumes: `require_admin`, `sb_get/sb_post/sb_patch`, `gotrue(..., admin=True)`.
- Produces: `GET /api/admin/members -> [{user_id, email, name, role, active, balance, created_at}]`, `POST /api/admin/members {email, password, name} -> {user_id}`, `PATCH /api/admin/members/{user_id} {active} -> {ok}`, `POST /api/admin/credits {user_id, delta} -> {balance}`.

- [ ] **Step 1: Write `scripts/create_admin.py`**

```python
"""One-time admin bootstrap. Usage:
python scripts/create_admin.py admin@example.com "StrongPass123!"
Reads SUPABASE_PROJECT_URL + SUPABASE_SERVICE_ROLE_KEY from vercel-app/.env.deploy or env."""
from __future__ import annotations
import os, sys
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]

def load_env(p: Path) -> None:
    if not p.exists(): return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip())

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
        r.raise_for_status(); uid = r.json()["id"]
    pr = httpx.post(f"{url}/rest/v1/profiles", headers={**h, "Prefer": "resolution=merge-duplicates"},
                    json={"user_id": uid, "role": "admin", "name": "Admin", "active": True},
                    params={"on_conflict": "user_id"}, timeout=20)
    pr.raise_for_status()
    print(f"admin ready: {email} ({uid})")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run it, then apply migration 003**

**Checkpoint — ask the user for the admin email + password to use** (do not invent live credentials).
Run: `python scripts/create_admin.py <email> <password>` → `admin ready: ...`
Run: `python scripts/apply_migrations.py 003` → `done`.
Verify: curl PostgREST `web_scraper_leads?select=user_id&limit=1` with service key → row has non-null `user_id`.

- [ ] **Step 3: Implement `_admin.py`**

```python
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
    if not rows: raise HTTPException(404, "no such member")
    return {"ok": True}

@router.post("/credits")
def adjust_credits(body: CreditsIn):
    if body.delta == 0: raise HTTPException(400, "delta must be non-zero")
    sb_post("credits_ledger", {"user_id": body.user_id, "delta": body.delta, "reason": "admin_adjust"},
            prefer="return=minimal")
    bal = sb_get("credit_balances", {"user_id": f"eq.{body.user_id}", "select": "balance"})
    return {"balance": bal[0]["balance"] if bal else 0}
```

Mount in `index.py`: `from _admin import router as admin_router` + `app.include_router(admin_router)`.

- [ ] **Step 4: Admin tab UI**

In `vercel-app/index.html`:
- Nav (after `id="tab-btn-leads"` button): `<button class="tab" id="tab-btn-admin" style="display:none" onclick="showTab('admin')">Admin</button>`
- After `<main id="tab-leads" ...>...</main>` add:

```html
<main id="tab-admin" style="display:none;grid-template-columns:1fr">
  <div class="card">
    <h2>Members</h2>
    <form id="new-member" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
      <input id="nm-email" type="email" placeholder="email" required>
      <input id="nm-pass" type="text" placeholder="temp password" required minlength="8">
      <input id="nm-name" type="text" placeholder="name">
      <button class="btn" type="submit">Create member</button>
    </form>
    <div id="members-tbl"></div>
  </div>
</main>
```

- Extend `showTab`: add `$('#tab-admin').style.display = t === 'admin' ? 'grid' : 'none';`, `$('#tab-btn-admin').classList.toggle('active', t === 'admin');`, and `if (t === 'admin') loadMembers();`
- JS:

```javascript
async function loadMembers() {
  const rows = await authApi('/api/admin/members');
  $('#members-tbl').innerHTML = '<table><thead><tr><th>email</th><th>name</th><th>role</th><th>credits</th><th>active</th><th></th></tr></thead><tbody>' +
    rows.map(m => `<tr><td>${m.email}</td><td>${m.name||''}</td><td>${m.role}</td>
      <td>${m.balance} <a style="cursor:pointer;color:var(--brand2)" onclick="adjCredits('${m.user_id}')">±</a></td>
      <td>${m.active ? '✓' : '✗'}</td>
      <td><a style="cursor:pointer;color:var(--brand2)" onclick="toggleMember('${m.user_id}', ${!m.active})">${m.active?'deactivate':'activate'}</a></td></tr>`).join('') +
    '</tbody></table>';
}
window.adjCredits = async uid => {
  const d = parseInt(prompt('Credits delta (e.g. 500 or -200):') || '0', 10);
  if (d) { await authApi('/api/admin/credits', {method:'POST', body: JSON.stringify({user_id: uid, delta: d})}); loadMembers(); }
};
window.toggleMember = async (uid, active) => {
  await authApi('/api/admin/members/' + uid, {method:'PATCH', body: JSON.stringify({active})}); loadMembers();
};
document.getElementById('new-member').addEventListener('submit', async e => {
  e.preventDefault();
  await authApi('/api/admin/members', {method:'POST', body: JSON.stringify(
    {email: $('#nm-email').value, password: $('#nm-pass').value, name: $('#nm-name').value})});
  e.target.reset(); loadMembers();
});
```

- In the `/api/me` boot handler from Task 3, add: `if (m.role === 'admin') $('#tab-btn-admin').style.display = '';`

- [ ] **Step 5: Verify**

`python -m pytest tests/cloud -v` → PASS; import check clean. curl: `POST /api/admin/members` without token → 401.

- [ ] **Step 6: Commit**

```bash
git add scripts/create_admin.py vercel-app/api vercel-app/index.html
git commit -m "feat: admin bootstrap script, member management API + Admin tab"
```

---

### Task 5: Settings — webhook config, AI keys, agent tokens

**Files:**
- Modify: `vercel-app/api/_accounts.py` (add settings + agent-token endpoints)
- Modify: `vercel-app/index.html` (Settings tab)
- Test: `tests/cloud/test_auth.py` (add secret-generation test)

**Interfaces:**
- Produces: `GET /api/settings -> {webhook_url, webhook_secret, gemini_key_masked, openai_key_masked}`, `PUT /api/settings {webhook_url?, gemini_key?, openai_key?} -> {ok}` (absent field = unchanged), `POST /api/settings/webhook-secret -> {webhook_secret}`, `GET /api/agent-tokens -> [{id,label,last_seen_at,revoked,created_at}]`, `POST /api/agent-tokens {label} -> {token}` (plaintext shown once), `POST /api/agent-tokens/{id}/revoke -> {ok}`. Helper `new_secret() -> str` (`secrets.token_urlsafe(32)`).
- Consumes: `current_user`, `mask`, `hash_token`, `sb_*`.

- [ ] **Step 1: Failing test**

Append to `tests/cloud/test_auth.py`:
```python
def test_new_secret_len_and_uniqueness():
    from _accounts import new_secret
    a, b = new_secret(), new_secret()
    assert a != b and len(a) >= 32
```
Run: `python -m pytest tests/cloud/test_auth.py -v` → FAIL (`ImportError`).

- [ ] **Step 2: Implement endpoints in `_accounts.py`**

```python
import secrets
from _auth import mask, hash_token

def new_secret() -> str:
    return secrets.token_urlsafe(32)

class SettingsIn(BaseModel):
    webhook_url: str | None = None
    gemini_key: str | None = None
    openai_key: str | None = None

def _settings_row(user_id: str) -> dict:
    rows = sb_get("user_settings", {"user_id": f"eq.{user_id}", "select": "*"})
    if rows: return rows[0]
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
```

- [ ] **Step 3: Run tests**

`python -m pytest tests/cloud -v` → PASS.

- [ ] **Step 4: Settings tab UI**

Nav: `<button class="tab" id="tab-btn-settings" onclick="showTab('settings')">Settings</button>` (always visible). New main:

```html
<main id="tab-settings" style="display:none;grid-template-columns:1fr">
  <div class="card" style="max-width:720px">
    <h2>Webhook</h2>
    <p class="muted">Every verified lead is POSTed here as JSON with an <code>X-Signature</code> HMAC-SHA256 header.</p>
    <div style="display:flex;gap:8px"><input id="set-webhook" style="flex:1" placeholder="https://your-endpoint.example/hook">
      <button class="btn" onclick="saveSettings()">Save</button>
      <button class="btn" onclick="testWebhook()">Send test</button></div>
    <div class="muted" style="margin-top:6px">Secret: <code id="set-secret"></code>
      <a style="cursor:pointer;color:var(--brand2)" onclick="regenSecret()">regenerate</a></div>
    <h3>Recent deliveries</h3><div id="deliveries-tbl"></div>
    <h2 style="margin-top:20px">AI keys (yours, used server-side only)</h2>
    <div style="display:flex;gap:8px;flex-direction:column;max-width:520px">
      <input id="set-gemini" placeholder="Gemini API key">
      <input id="set-openai" placeholder="OpenAI API key">
      <div><button class="btn" onclick="saveKeys()">Save keys</button> <span class="muted" id="keys-state"></span></div>
    </div>
    <h2 style="margin-top:20px">Agent tokens</h2>
    <p class="muted">Run the scraper on your PC: <code>python -m webscraper agent --token &lt;token&gt;</code></p>
    <button class="btn" onclick="newToken()">New token</button>
    <div id="tokens-tbl" style="margin-top:8px"></div>
    <h2 style="margin-top:20px">Password</h2>
    <div style="display:flex;gap:8px"><input id="set-pass" type="password" placeholder="new password" minlength="8">
      <button class="btn" onclick="changePass()">Change</button></div>
  </div>
</main>
```

JS (`showTab` gains `settings` case, calls `loadSettings()` on show):

```javascript
async function loadSettings() {
  const s = await authApi('/api/settings');
  $('#set-webhook').value = s.webhook_url || '';
  $('#set-secret').textContent = s.webhook_secret || '';
  $('#keys-state').textContent = ['Gemini: ' + (s.gemini_key_masked || 'not set'),
                                  'OpenAI: ' + (s.openai_key_masked || 'not set')].join(' · ');
  const toks = await authApi('/api/agent-tokens');
  $('#tokens-tbl').innerHTML = toks.length ? '<table><thead><tr><th>label</th><th>last seen</th><th>status</th><th></th></tr></thead><tbody>' +
    toks.map(t => `<tr><td>${t.label||('#'+t.id)}</td><td>${t.last_seen_at||'never'}</td><td>${t.revoked?'revoked':'active'}</td>
      <td>${t.revoked?'':`<a style="cursor:pointer;color:var(--brand2)" onclick="revokeToken(${t.id})">revoke</a>`}</td></tr>`).join('') +
    '</tbody></table>' : '<div class="muted">no tokens yet</div>';
  const dels = await authApi('/api/webhooks/deliveries').catch(()=>[]);
  $('#deliveries-tbl').innerHTML = dels.length ? '<table><thead><tr><th>lead</th><th>status</th><th>attempts</th><th>error</th></tr></thead><tbody>' +
    dels.map(d => `<tr><td>${d.lead_place_key}</td><td>${d.status}</td><td>${d.attempts}</td><td>${d.last_error||''}</td></tr>`).join('') +
    '</tbody></table>' : '<div class="muted">no deliveries yet</div>';
}
window.saveSettings = async () => { await authApi('/api/settings', {method:'PUT', body: JSON.stringify({webhook_url: $('#set-webhook').value})}); loadSettings(); };
window.saveKeys = async () => {
  const b = {}; if ($('#set-gemini').value) b.gemini_key = $('#set-gemini').value;
  if ($('#set-openai').value) b.openai_key = $('#set-openai').value;
  await authApi('/api/settings', {method:'PUT', body: JSON.stringify(b)});
  $('#set-gemini').value = ''; $('#set-openai').value = ''; loadSettings();
};
window.regenSecret = async () => { await authApi('/api/settings/webhook-secret', {method:'POST'}); loadSettings(); };
window.newToken = async () => {
  const t = await authApi('/api/agent-tokens', {method:'POST', body: JSON.stringify({label: prompt('Token label (e.g. office PC):')||null})});
  prompt('Copy this token now — it is shown only once:', t.token); loadSettings();
};
window.revokeToken = async id => { await authApi(`/api/agent-tokens/${id}/revoke`, {method:'POST'}); loadSettings(); };
window.changePass = async () => { await authApi('/api/me/password', {method:'POST', body: JSON.stringify({password: $('#set-pass').value})}); $('#set-pass').value=''; alert('changed'); };
window.testWebhook = async () => { const r = await authApi('/api/webhooks/test', {method:'POST'}); alert(r.ok ? 'delivered ✓' : ('failed: ' + (r.error||''))); };
```

(`/api/webhooks/test` + `/api/webhooks/deliveries` land in Task 9 — buttons 404 until then; the `.catch(()=>[])` keeps the tab usable.)

- [ ] **Step 5: Verify + commit**

`python -m pytest tests/cloud -v` → PASS; import check clean.
```bash
git add vercel-app/api/_accounts.py vercel-app/index.html tests/cloud/test_auth.py
git commit -m "feat: member settings — webhook config, AI keys, agent tokens, Settings tab"
```

---

### Task 6: Cloud job queue — browser side

**Files:**
- Create: `vercel-app/api/_jobs.py`
- Modify: `vercel-app/api/index.py` (mount; delete the old `/api/jobs` stub + `_disabled` handlers)
- Modify: `vercel-app/index.html` (re-enable Jobs tab in cloud, wire to new endpoints)

**Interfaces:**
- Produces: `POST /api/jobs {query, location?, lat?, lng?, radius_km?, limit_places?, country?} -> job row`, `GET /api/jobs -> [job rows]` (own; admin: all), `GET /api/jobs/{id} -> row`, `POST /api/jobs/{id}/cancel -> {ok}`. Job row shape = `scrape_jobs` columns.
- Consumes: `current_user`, `sb_*`. Task 7's agent endpoints operate on the same rows.

- [ ] **Step 1: Implement `_jobs.py`**

```python
"""Browser-facing job queue CRUD."""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from _auth import User, current_user
from _db import sb_get, sb_patch, sb_post

router = APIRouter(prefix="/api")

class JobIn(BaseModel):
    query: str = Field(min_length=2)
    location: str | None = None
    lat: float | None = None
    lng: float | None = None
    radius_km: float | None = None
    limit_places: int = Field(default=100, ge=1, le=5000)
    country: str | None = None

@router.post("/jobs")
def create_job(body: JobIn, user: User = Depends(current_user)):
    rows = sb_post("scrape_jobs", {**body.model_dump(), "user_id": user.id})
    return rows[0]

@router.get("/jobs")
def list_jobs(user: User = Depends(current_user)):
    params = {"select": "*", "order": "id.desc", "limit": "200"}
    if user.role != "admin":
        params["user_id"] = f"eq.{user.id}"
    return sb_get("scrape_jobs", params)

@router.get("/jobs/{job_id}")
def get_job(job_id: int, user: User = Depends(current_user)):
    params = {"id": f"eq.{job_id}", "select": "*"}
    if user.role != "admin":
        params["user_id"] = f"eq.{user.id}"
    rows = sb_get("scrape_jobs", params)
    if not rows: raise HTTPException(404, "no such job")
    return rows[0]

@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, user: User = Depends(current_user)):
    params = {"id": f"eq.{job_id}"}
    if user.role != "admin":
        params["user_id"] = f"eq.{user.id}"
    rows = sb_patch("scrape_jobs", {"status": "cancelled"}, params)
    if not rows: raise HTTPException(404, "no such job")
    return {"ok": True}
```

Mount in `index.py`; **remove** the old `@app.get("/api/jobs")` stub returning `[]` and the `_disabled` POST handlers (`/api/jobs`, `/api/supabase/sync`).

- [ ] **Step 2: Re-enable Jobs tab in cloud UI**

In `vercel-app/index.html`:
- In `applyCloudMode()`: **delete** the lines hiding the scraper tab and rewriting the New button (`const st = $('#tab-btn-scraper'); if (st) st.style.display = 'none';`, the `nb.textContent = 'ⓘ Scraper runs locally'` block, and the forced `showTab('leads', true)`). Keep `document.body.classList.add('cloud')` and the from-Supabase checkbox lines.
- In `refresh()`: replace the cloud early-return branch with cloud job polling:

```javascript
if (window.CLOUD) {
  jobs = await api('/api/jobs').catch(()=>[]);
  renderCloudJobs();
  pollTimer = setTimeout(refresh, 5000);
  return;
}
```

- Add `renderCloudJobs` (next to `renderJobs`):

```javascript
function renderCloudJobs() {
  if (!jobs.length) { $('#jobs').innerHTML = '<div class="muted">none yet</div>'; return; }
  const label = s => ({queued:'⏳ queued — start your agent', claimed:'🤝 claimed', running:'▶ running',
    paused_quota:'⛔ out of credits', done:'✓ done', error:'✗ error', cancelled:'∅ cancelled'}[s] || s);
  $('#jobs').innerHTML = jobs.map(j => {
    const p = j.progress || {};
    return `<div class="job"><b>#${j.id}</b> ${j.query}${j.location ? ' · ' + j.location : ''}
      <div class="muted">${label(j.status)}${j.phase ? ' · ' + j.phase : ''}
        ${p.scraped_count != null ? ` · ${p.scraped_count} scraped` : ''}${p.enrich_done != null ? ` · ${p.enrich_done}/${p.enrich_total||0} enriched` : ''}</div>
      ${['queued','claimed','running'].includes(j.status) ? `<a style="cursor:pointer;color:var(--brand2)" onclick="cancelCloudJob(${j.id})">cancel</a>` : ''}</div>`;
  }).join('');
}
window.cancelCloudJob = async id => { await api(`/api/jobs/${id}/cancel`, {method:'POST'}); refresh(); };
```

- Job creation: inside `saveJob()` branch for cloud:

```javascript
if (window.CLOUD) {
  const b = jobBody();
  await api('/api/jobs', {method:'POST', body: JSON.stringify({query: b.query, location: b.location,
    lat: b.center_lat, lng: b.center_lng, radius_km: b.radius_km, limit_places: b.max_places, country: b.country})});
  closeJobDialog(); refresh(); return;
}
```

- [ ] **Step 3: Verify**

Import check + `python -m pytest tests/cloud -v` → PASS. Local uvicorn: `curl -s -X POST http://127.0.0.1:8899/api/jobs -H "Content-Type: application/json" -d "{\"query\":\"cafe\"}"` → 401. `curl -s http://127.0.0.1:8899/ | grep -c tab-btn-scraper` → 1.

- [ ] **Step 4: Commit**

```bash
git add vercel-app/api vercel-app/index.html
git commit -m "feat: cloud job queue API + Jobs tab restored in cloud UI"
```

---

### Task 7: Local agent — poll, claim, run, report

**Files:**
- Create: `vercel-app/api/_agent.py` (poll/claim/progress/done; `sync` added in Task 8)
- Create: `webscraper/agent.py`
- Modify: `webscraper/cli.py` (new `agent` command)
- Modify: `webscraper/store.py` (`cloud_id` column in `_migrate`)
- Modify: `vercel-app/api/index.py` (mount)
- Test: `tests/cloud/test_agent_rules.py`

**Interfaces:**
- Produces (cloud): `GET /api/agent/jobs -> [scrape_jobs rows]` (own `queued` + own in-flight + stale claims), `POST /api/agent/jobs/{id}/claim -> row` (409 if fresh claim by another agent), `POST /api/agent/jobs/{id}/progress {phase, progress} -> {ok, cancelled: bool}`, `POST /api/agent/jobs/{id}/done {status: "done"|"error", error?} -> {ok}`. Pure fn `claim_is_stale(updated_at_iso: str, now: datetime) -> bool` (>30 min).
- Produces (local): CLI `python -m webscraper agent --token <tok> [--base <url>] [--poll 20]`; `webscraper/agent.py` exposes `run_agent(base: str, token: str, poll_sec: int) -> None`. `Store._migrate` gains `("cloud_id", "INTEGER")` on jobs.
- Consumes: `agent_user` dep (Task 2), local `Worker` (`webscraper/server.py:58`), `Store.places(job_id)` (Task 8 sync), `supa._row` for flattening.

- [ ] **Step 1: Failing test for the stale-claim rule**

`tests/cloud/test_agent_rules.py`:
```python
from datetime import datetime, timedelta, timezone
from _agent import claim_is_stale

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)

def test_fresh_claim_not_stale():
    assert not claim_is_stale((NOW - timedelta(minutes=10)).isoformat(), NOW)

def test_old_claim_is_stale():
    assert claim_is_stale((NOW - timedelta(minutes=31)).isoformat(), NOW)

def test_z_suffix_parses():
    assert claim_is_stale("2026-08-21T11:00:00Z", NOW)
```
Run: `python -m pytest tests/cloud/test_agent_rules.py -v` → FAIL (module missing).

- [ ] **Step 2: Implement `_agent.py`**

```python
"""Endpoints for the member's local agent (X-Agent-Token auth)."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from _auth import User, agent_user
from _db import sb_get, sb_patch

router = APIRouter(prefix="/api/agent")
STALE_MIN = 30

def claim_is_stale(updated_at_iso: str, now: datetime) -> bool:
    ts = datetime.fromisoformat(updated_at_iso.replace("Z", "+00:00"))
    return (now - ts) > timedelta(minutes=STALE_MIN)

@router.get("/jobs")
def agent_jobs(user: User = Depends(agent_user)):
    rows = sb_get("scrape_jobs", {"user_id": f"eq.{user.id}",
                                  "status": "in.(queued,claimed,running)",
                                  "select": "*", "order": "id.asc"})
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        if r["status"] == "queued":
            out.append(r)
        elif r["claimed_by"] == user.agent_token_id or claim_is_stale(r["updated_at"], now):
            out.append(r)   # own in-flight job (resume) or stale claim from a dead agent
    return out

@router.post("/jobs/{job_id}/claim")
def claim(job_id: int, user: User = Depends(agent_user)):
    rows = sb_get("scrape_jobs", {"id": f"eq.{job_id}", "user_id": f"eq.{user.id}", "select": "*"})
    if not rows: raise HTTPException(404, "no such job")
    j = rows[0]
    now = datetime.now(timezone.utc)
    if j["status"] in ("claimed", "running") and j["claimed_by"] not in (None, user.agent_token_id) \
       and not claim_is_stale(j["updated_at"], now):
        raise HTTPException(409, "claimed by another agent")
    if j["status"] not in ("queued", "claimed", "running"):
        raise HTTPException(409, f"job is {j['status']}")
    upd = sb_patch("scrape_jobs", {"status": "claimed", "claimed_by": user.agent_token_id,
                                   "updated_at": datetime.now(timezone.utc).isoformat()},
                   {"id": f"eq.{job_id}"})
    return upd[0]

class ProgressIn(BaseModel):
    phase: str | None = None
    progress: dict = {}

@router.post("/jobs/{job_id}/progress")
def progress(job_id: int, body: ProgressIn, user: User = Depends(agent_user)):
    rows = sb_get("scrape_jobs", {"id": f"eq.{job_id}", "user_id": f"eq.{user.id}", "select": "status"})
    if not rows: raise HTTPException(404, "no such job")
    if rows[0]["status"] == "cancelled":
        return {"ok": True, "cancelled": True}
    sb_patch("scrape_jobs", {"status": "running", "phase": body.phase, "progress": body.progress,
                             "updated_at": datetime.now(timezone.utc).isoformat()},
             {"id": f"eq.{job_id}"})
    return {"ok": True, "cancelled": False}

class DoneIn(BaseModel):
    status: str  # done | error
    error: str | None = None

@router.post("/jobs/{job_id}/done")
def done(job_id: int, body: DoneIn, user: User = Depends(agent_user)):
    if body.status not in ("done", "error"): raise HTTPException(400, "status must be done|error")
    cur = sb_get("scrape_jobs", {"id": f"eq.{job_id}", "user_id": f"eq.{user.id}", "select": "status"})
    if not cur: raise HTTPException(404, "no such job")
    if cur[0]["status"] == "paused_quota" and body.status == "done":
        return {"ok": True}   # sync already flagged quota exhaustion; keep that status
    sb_patch("scrape_jobs", {"status": body.status, "error": body.error,
                             "updated_at": datetime.now(timezone.utc).isoformat()},
             {"id": f"eq.{job_id}"})
    return {"ok": True}
```

Mount in `index.py`.
(Note: PostgREST does not interpret the literal string `now()` in a PATCH body as SQL — always send real ISO timestamps as above. Task 8/9 code follows the same rule.)

- [ ] **Step 3: Run cloud tests**

`python -m pytest tests/cloud -v` → PASS.

- [ ] **Step 4: `store.py` cloud_id migration**

In `webscraper/store.py:_migrate`, jobs-columns tuple (anchor `("scrape_started_at", "TEXT"),`): add `("cloud_id", "INTEGER"),`.

- [ ] **Step 5: Implement `webscraper/agent.py`**

```python
"""Agent mode: mirror cloud jobs into the local pipeline and report back.

Reuses the local Worker (webscraper/server.py) untouched: each cloud job becomes a local
jobs row (phase 'queued', cloud_id set); the Worker thread picks it up exactly as if the
local UI had created it. This loop watches local state and mirrors it up.
"""
from __future__ import annotations
import logging
import time
from typing import Any

import httpx

from webscraper.store import Store
from webscraper import server as srv

log = logging.getLogger("webscraper.agent")


class Cloud:
    def __init__(self, base: str, token: str):
        self.c = httpx.Client(base_url=base.rstrip("/"), timeout=60,
                              headers={"X-Agent-Token": token, "Content-Type": "application/json"})

    def jobs(self) -> list[dict]:
        r = self.c.get("/api/agent/jobs"); r.raise_for_status(); return r.json()

    def claim(self, jid: int) -> dict | None:
        r = self.c.post(f"/api/agent/jobs/{jid}/claim")
        if r.status_code == 409: return None
        r.raise_for_status(); return r.json()

    def progress(self, jid: int, phase: str | None, progress: dict) -> bool:
        """Returns True if the job was cancelled cloud-side."""
        r = self.c.post(f"/api/agent/jobs/{jid}/progress", json={"phase": phase, "progress": progress})
        r.raise_for_status(); return bool(r.json().get("cancelled"))

    def done(self, jid: int, status: str, error: str | None = None) -> None:
        self.c.post(f"/api/agent/jobs/{jid}/done", json={"status": status, "error": error}).raise_for_status()

    def sync(self, jid: int, rows: list[dict]) -> dict:
        r = self.c.post("/api/agent/sync", json={"cloud_job_id": jid, "rows": rows})
        r.raise_for_status(); return r.json()


def _flat(r: dict) -> dict:
    from webscraper.supa import _row
    return _row(r)


def _local_progress(row: Any) -> dict:
    return {"scraped_count": row["scraped_count"], "links_found": row["links_found"],
            "enrich_done": row["enrich_done"], "enrich_total": row["enrich_total"]}


def run_agent(base: str, token: str, poll_sec: int = 20) -> None:
    cloud = Cloud(base, token)
    srv.worker.start()                       # same Worker the local UI uses
    store = Store()
    log.info("agent up — polling %s every %ss", base, poll_sec)
    while True:
        try:
            _tick(cloud, store)
        except httpx.HTTPError as e:
            log.warning("cloud unreachable: %s", e)
        time.sleep(poll_sec)


def _tick(cloud: Cloud, store: Store) -> None:
    mirrored = {r["cloud_id"] for r in store.conn.execute(
        "SELECT cloud_id FROM jobs WHERE cloud_id IS NOT NULL").fetchall()}
    for cj in cloud.jobs():
        if cj["id"] in mirrored:
            continue
        if cloud.claim(cj["id"]) is None:
            continue
        local_id = store.create_job(
            query=cj["query"], location=cj.get("location"),
            max_places=cj.get("limit_places") or 100, delay_sec=0,
            phase="queued", do_enrich=True, headless=True, country=cj.get("country"),
            radius_km=cj.get("radius_km"),
            center_lat=cj.get("lat"), center_lng=cj.get("lng"))
        store.update_job(local_id, cloud_id=cj["id"])
        log.info("cloud job #%s -> local job #%s", cj["id"], local_id)
    # mirror running/finished local state up
    for row in store.conn.execute(
            "SELECT * FROM jobs WHERE cloud_id IS NOT NULL AND (note IS NULL OR note <> 'synced')").fetchall():
        cid = row["cloud_id"]
        if row["phase"] in ("scraping", "enriching", "queued", "waiting"):
            cancelled = cloud.progress(cid, row["phase"], _local_progress(row))
            if cancelled:
                store.update_job(row["id"], stop_requested=1, note="synced")
        elif row["phase"] in ("done", "stopped", "failed"):
            rows = store.places(row["id"])
            quota_hit = False
            for i in range(0, len(rows), 200):
                res = cloud.sync(cid, [_flat(r) for r in rows[i:i + 200]])
                log.info("sync job #%s: accepted %s, rejected_quota %s",
                         cid, res.get("accepted"), res.get("rejected_quota"))
                if res.get("rejected_quota"):
                    quota_hit = True
                    break            # out of credits — retried on a later tick after top-up
            if not quota_hit:
                cloud.done(cid, "done" if row["phase"] == "done" else "error",
                           None if row["phase"] == "done" else row["phase"])
                store.update_job(row["id"], note="synced")
```

- [ ] **Step 6: CLI command**

In `webscraper/cli.py` append:

```python
@app.command()
def agent(
    token: str = typer.Option(..., "--token", help="Agent token from the cloud Settings tab"),
    base: str = typer.Option("https://web-scraper-leads.vercel.app", "--base"),
    poll: int = typer.Option(20, "--poll", help="Seconds between cloud polls"),
) -> None:
    """Run cloud jobs on this machine: poll -> scrape locally -> sync results up."""
    from webscraper.agent import run_agent
    run_agent(base, token, poll)
```

- [ ] **Step 7: Verify**

`python -m pytest tests -v` → all PASS (extractor + cloud tests).
`python -m webscraper agent --help` → renders. `python -c "from webscraper.agent import run_agent"` → clean.
(`/api/agent/sync` doesn't exist until Task 8 — the agent only calls it after a job finishes; full loop is exercised in Task 8 step 5.)

- [ ] **Step 8: Commit**

```bash
git add vercel-app/api/_agent.py webscraper/agent.py webscraper/cli.py webscraper/store.py tests/cloud/test_agent_rules.py vercel-app/api/index.py
git commit -m "feat: agent mode — cloud claim/progress endpoints + local poll loop"
```

---

### Task 8: Sync endpoint — verify, debit, upsert + user-scoped leads

**Files:**
- Modify: `vercel-app/api/_agent.py` (add `/api/agent/sync` + pure helpers)
- Create: `vercel-app/api/_leads.py`
- Modify: `vercel-app/api/index.py` (mount `_leads`, remove old `/api/leads` handler)
- Test: `tests/cloud/test_sync_rules.py`

**Interfaces:**
- Produces:
  - Pure fns in `_agent.py`: `is_verified(row: dict) -> bool`, `partition_new(rows: list[dict], existing_keys: set[str]) -> tuple[list, list]`.
  - `POST /api/agent/sync {cloud_job_id, rows: [flattened lead dicts]} -> {accepted: int, rejected_quota: int, debited: int}`. Behavior: drop rows without `place_key`; split new vs existing by `(user_id, place_key)`; existing rows re-upserted free (no debit, no webhook); new verified rows debit via `debit_credits` RPC — first `debited` accepted, rest rejected; new unverified rows accepted free; `rejected_quota > 0` → job `paused_quota`; accepted rows upserted with `user_id`, `cloud_job_id`, `verified`; newly-inserted verified leads → `enqueue_and_deliver(user_id, place_keys)` from `_webhooks` (guarded `try/except ImportError` until Task 9).
  - `_leads.py`: `GET /api/leads?limit=5000 -> {total, rows, source: "supabase"}` — member: own rows; admin: all.
- Consumes: `agent_user`, `debit_credits` RPC (Task 1), flattened-row shape from `supa._COLS`.

- [ ] **Step 1: Failing tests**

`tests/cloud/test_sync_rules.py`:
```python
from _agent import is_verified, partition_new

def test_verified_needs_contact_and_enrichment():
    assert is_verified({"enrich_status": "done", "phone": "+911234", "email": None})
    assert is_verified({"enrich_status": "no_website", "phone": None, "email": "a@b.c"})
    assert not is_verified({"enrich_status": "pending", "phone": "+911234"})
    assert not is_verified({"enrich_status": "done", "phone": "", "email": ""})
    assert not is_verified({"enrich_status": None, "phone": "+911234"})

def test_partition_new_splits_on_existing_keys():
    rows = [{"place_key": "a"}, {"place_key": "b"}, {"place_key": "c"}]
    new, old = partition_new(rows, {"b"})
    assert [r["place_key"] for r in new] == ["a", "c"]
    assert [r["place_key"] for r in old] == ["b"]
```
Run → FAIL.

- [ ] **Step 2: Implement sync in `_agent.py`**

Add imports `from _db import sb_post, sb_rpc` and:

```python
# ── sync ─────────────────────────────────────────────────────────────────────
FINISHED = {"done", "thin", "failed", "no_website"}

def is_verified(row: dict) -> bool:
    if (row.get("enrich_status") or "pending") not in FINISHED:
        return False
    return bool((row.get("phone") or "").strip() or (row.get("email") or "").strip())

def partition_new(rows: list[dict], existing_keys: set[str]) -> tuple[list[dict], list[dict]]:
    new = [r for r in rows if r["place_key"] not in existing_keys]
    old = [r for r in rows if r["place_key"] in existing_keys]
    return new, old

class SyncIn(BaseModel):
    cloud_job_id: int
    rows: list[dict]

@router.post("/sync")
def sync(body: SyncIn, user: User = Depends(agent_user)):
    job = sb_get("scrape_jobs", {"id": f"eq.{body.cloud_job_id}", "user_id": f"eq.{user.id}", "select": "id,status"})
    if not job: raise HTTPException(404, "no such job")
    rows = [r for r in body.rows if r.get("place_key")]
    if not rows: return {"accepted": 0, "rejected_quota": 0, "debited": 0}
    keys = ",".join('"' + r["place_key"].replace('"', "") + '"' for r in rows)
    existing = {e["place_key"] for e in sb_get("web_scraper_leads",
                {"user_id": f"eq.{user.id}", "place_key": f"in.({keys})", "select": "place_key"})}
    new, old = partition_new(rows, existing)
    new_verified = [r for r in new if is_verified(r)]
    new_free = [r for r in new if not is_verified(r)]
    debited = sb_rpc("debit_credits", {"p_user": user.id, "p_requested": len(new_verified),
                                       "p_job": body.cloud_job_id}) or 0
    accepted_verified = new_verified[:debited]
    rejected = len(new_verified) - debited
    payload = []
    for r in accepted_verified:
        payload.append({**r, "user_id": user.id, "cloud_job_id": body.cloud_job_id, "verified": True})
    for r in new_free + old:
        payload.append({**r, "user_id": user.id, "cloud_job_id": body.cloud_job_id,
                        "verified": is_verified(r)})   # old rows refresh their flag; no re-debit
    for i in range(0, len(payload), 200):
        sb_post("web_scraper_leads", payload[i:i + 200],
                params={"on_conflict": "user_id,place_key"},
                prefer="resolution=merge-duplicates,return=minimal")
    if rejected:
        sb_patch("scrape_jobs", {"status": "paused_quota"}, {"id": f"eq.{body.cloud_job_id}"})
    try:
        from _webhooks import enqueue_and_deliver
        enqueue_and_deliver(user.id, [r["place_key"] for r in accepted_verified])
    except ImportError:
        pass   # Task 9 adds _webhooks
    return {"accepted": len(payload), "rejected_quota": rejected, "debited": debited}
```

- [ ] **Step 3: Implement `_leads.py`**

```python
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
```

Mount in `index.py`; delete the old `GET /api/leads` handler there.

- [ ] **Step 4: Run tests**

`python -m pytest tests/cloud -v` → PASS.

- [ ] **Step 5: End-to-end agent smoke (local uvicorn + real Supabase)**

With env loaded, uvicorn on :8899. Login as admin → create agent token (`POST /api/agent-tokens`), self-credit (`POST /api/admin/credits {delta: 50}`), create a tiny job (`POST /api/jobs {"query":"cafe","location":"Aundh Pune","limit_places":3}`), then:
`python -m webscraper agent --token <tok> --base http://127.0.0.1:8899 --poll 10`
Expected within minutes: job `claimed → running → done`; `GET /api/leads` shows rows with `verified`; `GET /api/me` balance dropped by verified count. (Drives a real headless scrape of ~3 places — acceptable.) Ctrl+C the agent.

- [ ] **Step 6: Commit**

```bash
git add vercel-app/api tests/cloud/test_sync_rules.py
git commit -m "feat: agent sync — verified-lead rule, credit debit, per-user leads"
```

---

### Task 9: Webhook dispatch + deliveries + cron re-drive

**Files:**
- Create: `vercel-app/api/_webhooks.py`
- Modify: `vercel-app/api/index.py` (mount)
- Modify: `vercel-app/vercel.json` (cron)
- Test: `tests/cloud/test_webhook_sig.py`

**Interfaces:**
- Produces: `sign(secret: str, body: bytes) -> str` (hex HMAC-SHA256), `enqueue_and_deliver(user_id: str, place_keys: list[str]) -> None`, `POST /api/webhooks/test -> {ok, error?}`, `GET /api/webhooks/deliveries -> [rows]`, `GET /api/cron/webhooks` (Bearer `CRON_SECRET`).
- Consumes: `user_settings.webhook_url/webhook_secret`, `webhook_deliveries`, lead rows.

- [ ] **Step 1: Failing test**

`tests/cloud/test_webhook_sig.py`:
```python
import hashlib, hmac
from _webhooks import sign

def test_sign_matches_manual_hmac():
    body = b'{"a":1}'
    assert sign("sec", body) == hmac.new(b"sec", body, hashlib.sha256).hexdigest()
```
Run → FAIL.

- [ ] **Step 2: Implement `_webhooks.py`**

```python
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
    rows = sb_get("web_scraper_leads", {"user_id": f"eq.{user_id}", "place_key": f"eq.{place_key}", "select": "*"})
    if not rows: return None
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
    if not s: raise HTTPException(400, "set a webhook URL first")
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
```

Mount in `index.py`.

- [ ] **Step 3: Cron config**

`vercel-app/vercel.json` — add:
```json
"crons": [{ "path": "/api/cron/webhooks", "schedule": "0 3 * * *" }]
```
Vercel Hobby crons are daily-only; for 10-minute re-drives an n8n Schedule workflow on the HVT instance can hit `GET /api/cron/webhooks` with `Authorization: Bearer <CRON_SECRET>` — **user decision later, do not create it in this plan**.

- [ ] **Step 4: Tests + verify**

`python -m pytest tests/cloud -v` → PASS. curl: `GET /api/cron/webhooks` without header → 401. The Task 5 Settings tab now loads deliveries without 404.

- [ ] **Step 5: Commit**

```bash
git add vercel-app/api vercel-app/vercel.json tests/cloud/test_webhook_sig.py
git commit -m "feat: signed per-lead webhooks with retries, delivery log, cron re-drive"
```

---

### Task 10: Razorpay checkout

**Files:**
- Create: `vercel-app/api/_pay.py` (Razorpay half)
- Modify: `vercel-app/api/index.py` (mount)
- Modify: `vercel-app/index.html` (Billing tab + checkout.js)
- Test: `tests/cloud/test_pay.py`

**Interfaces:**
- Produces: `verify_rzp_signature(key_secret, order_id, payment_id, signature) -> bool`; `credit_order(order_row: dict) -> None` (idempotent); `POST /api/pay/razorpay/order {pack} -> {key_id, order_id, amount, currency, pack}`, `POST /api/pay/razorpay/verify {razorpay_order_id, razorpay_payment_id, razorpay_signature} -> {ok, balance}`, `POST /api/pay/razorpay/webhook` (raw body + `X-Razorpay-Signature`).
- Consumes: `PACKS`, `orders`, `credits_ledger`, env `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`.

- [ ] **Step 1: Failing tests**

`tests/cloud/test_pay.py`:
```python
import hashlib, hmac
from _pay import verify_rzp_signature

def test_rzp_signature_roundtrip():
    sig = hmac.new(b"secret", b"order_1|pay_1", hashlib.sha256).hexdigest()
    assert verify_rzp_signature("secret", "order_1", "pay_1", sig)
    assert not verify_rzp_signature("secret", "order_1", "pay_1", "deadbeef")
    assert not verify_rzp_signature("other", "order_1", "pay_1", sig)
```
Run → FAIL.

- [ ] **Step 2: Implement Razorpay half of `_pay.py`**

```python
"""Payments: Razorpay (this task) + PayU (Task 11). Amounts come from PACKS only."""
from __future__ import annotations
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from _auth import User, current_user
from _db import PACKS, sb_get, sb_patch, sb_post

router = APIRouter(prefix="/api/pay")

def verify_rzp_signature(key_secret: str, order_id: str, payment_id: str, signature: str) -> bool:
    expect = hmac.new(key_secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, signature or "")

def credit_order(order: dict) -> None:
    """Mark paid + add credits. Idempotent: ledger insert only when no ledger row exists."""
    if order["status"] == "paid":
        return
    sb_patch("orders", {"status": "paid", "paid_at": datetime.now(timezone.utc).isoformat()},
             {"id": f"eq.{order['id']}", "status": "neq.paid"})
    fresh = sb_get("orders", {"id": f"eq.{order['id']}", "select": "status"})
    ledger = sb_get("credits_ledger", {"order_id": f"eq.{order['id']}", "select": "id"})
    if fresh and fresh[0]["status"] == "paid" and not ledger:
        sb_post("credits_ledger", {"user_id": order["user_id"], "delta": order["leads"],
                                   "reason": "purchase", "order_id": order["id"]}, prefer="return=minimal")

class RzpOrderIn(BaseModel):
    pack: str

@router.post("/razorpay/order")
def rzp_order(body: RzpOrderIn, user: User = Depends(current_user)):
    if body.pack not in PACKS: raise HTTPException(400, "unknown pack")
    p = PACKS[body.pack]
    kid, ks = os.getenv("RAZORPAY_KEY_ID", ""), os.getenv("RAZORPAY_KEY_SECRET", "")
    if not kid or not ks: raise HTTPException(503, "Razorpay not configured")
    r = httpx.post("https://api.razorpay.com/v1/orders", auth=(kid, ks), timeout=20,
                   json={"amount": p["amount_inr"], "currency": "INR",
                         "notes": {"user_id": user.id, "pack": body.pack}})
    if r.status_code >= 300: raise HTTPException(502, f"razorpay: {r.text[:200]}")
    oid = r.json()["id"]
    sb_post("orders", {"user_id": user.id, "pack": body.pack, "leads": p["leads"],
                       "amount_inr": p["amount_inr"], "gateway": "razorpay",
                       "gateway_order_id": oid, "raw": r.json()}, prefer="return=minimal")
    return {"key_id": kid, "order_id": oid, "amount": p["amount_inr"], "currency": "INR", "pack": body.pack}

class RzpVerifyIn(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str

@router.post("/razorpay/verify")
def rzp_verify(body: RzpVerifyIn, user: User = Depends(current_user)):
    if not verify_rzp_signature(os.getenv("RAZORPAY_KEY_SECRET", ""),
                                body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature):
        raise HTTPException(400, "bad signature")
    rows = sb_get("orders", {"gateway_order_id": f"eq.{body.razorpay_order_id}",
                             "user_id": f"eq.{user.id}", "select": "*"})
    if not rows: raise HTTPException(404, "no such order")
    credit_order(rows[0])
    bal = sb_get("credit_balances", {"user_id": f"eq.{user.id}", "select": "balance"})
    return {"ok": True, "balance": bal[0]["balance"] if bal else 0}

@router.post("/razorpay/webhook")
async def rzp_webhook(request: Request, x_razorpay_signature: str = Header(default="")):
    body = await request.body()
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
    if not secret: raise HTTPException(503, "webhook secret not configured")
    expect = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, x_razorpay_signature):
        raise HTTPException(400, "bad signature")
    evt = json.loads(body)
    if evt.get("event") == "payment.captured":
        oid = evt["payload"]["payment"]["entity"].get("order_id")
        rows = sb_get("orders", {"gateway_order_id": f"eq.{oid}", "select": "*"}) if oid else []
        if rows:
            credit_order(rows[0])
    return {"ok": True}
```

Mount in `index.py`.

- [ ] **Step 3: Billing tab UI**

Nav: `<button class="tab" id="tab-btn-billing" onclick="showTab('billing')">Billing</button>`. New main:

```html
<main id="tab-billing" style="display:none;grid-template-columns:1fr">
  <div class="card" style="max-width:720px">
    <h2>Buy lead credits</h2>
    <div id="packs" style="display:flex;gap:14px;flex-wrap:wrap"></div>
    <div class="muted" id="billing-msg" style="margin-top:10px"></div>
  </div>
</main>
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
```

JS (`showTab` gains `billing`, calls `loadPacks()` on show):

```javascript
async function loadPacks() {
  const cfg = await authApi('/api/config');
  $('#packs').innerHTML = cfg.packs.map(p => `
    <div class="card" style="min-width:220px">
      <h3>${p.label}</h3>
      <div style="font-size:22px;font-weight:700">$${p.usd} <span class="muted" style="font-size:13px">₹${(p.amount_inr/100).toFixed(0)}</span></div>
      <div class="muted">${p.leads.toLocaleString()} verified leads</div>
      <div style="display:flex;gap:8px;margin-top:10px">
        <button class="btn" onclick="buyRzp('${p.id}')">Razorpay</button>
        <button class="btn" onclick="buyPayU('${p.id}')">PayU</button>
      </div>
    </div>`).join('');
  const st = new URLSearchParams(location.hash.split('?')[1]||'').get('status');
  if (st) $('#billing-msg').textContent = st === 'ok' ? '✓ payment received — credits added' : '✗ payment failed';
}
window.buyRzp = async pack => {
  const o = await authApi('/api/pay/razorpay/order', {method:'POST', body: JSON.stringify({pack})});
  new Razorpay({key: o.key_id, order_id: o.order_id, amount: o.amount, currency: o.currency,
    name: 'Lead Finder', description: pack,
    handler: async resp => {
      const v = await authApi('/api/pay/razorpay/verify', {method:'POST', body: JSON.stringify(resp)});
      $('#billing-msg').textContent = v.ok ? `✓ credits added — balance ${v.balance}` : '✗ verification failed';
      authApi('/api/me').then(m => $('#whoami-credits').textContent = '· ' + m.balance + ' credits');
    }}).open();
};
window.buyPayU = pack => alert('PayU arrives in the next release');  // replaced in Task 11
```

- [ ] **Step 4: Tests + verify**

`python -m pytest tests/cloud -v` → PASS. curl: `POST /api/pay/razorpay/order` unauth → 401.
**Checkpoint — ask the user for Razorpay TEST key id/secret + webhook secret**; add to `vercel-app/.env.deploy` (gitignored) and later to Vercel env.

- [ ] **Step 5: Commit**

```bash
git add vercel-app/api tests/cloud/test_pay.py vercel-app/index.html
git commit -m "feat: Razorpay credit-pack checkout with server-side verify + webhook"
```

---

### Task 11: PayU checkout

**Files:**
- Modify: `vercel-app/api/_pay.py` (PayU half)
- Modify: `vercel-app/requirements.txt` (add `python-multipart`)
- Modify: `vercel-app/index.html` (real `buyPayU`)
- Test: `tests/cloud/test_pay.py` (extend)

**Interfaces:**
- Produces: `payu_request_hash(key, salt, txnid, amount, productinfo, firstname, email, udf1, udf2) -> str`, `payu_response_hash(key, salt, status, txnid, amount, productinfo, firstname, email, udf1, udf2) -> str`; `POST /api/pay/payu/initiate {pack} -> {action, fields}`, `POST /api/pay/payu/return` (form-POST; verifies reverse hash, credits, 303 → `/#billing?status=ok|failed`).
- Consumes: env `PAYU_KEY`, `PAYU_SALT`, `PAYU_BASE` (default `https://test.payu.in`), `APP_BASE_URL`.

- [ ] **Step 1: Failing tests**

Append to `tests/cloud/test_pay.py`:
```python
import hashlib
from _pay import payu_request_hash, payu_response_hash

ARGS = dict(key="K", salt="S", txnid="t1", amount="880.00", productinfo="starter_3k",
            firstname="Member", email="m@x.com", udf1="uid-1", udf2="starter_3k")

def test_payu_request_hash_formula():
    seq = "K|t1|880.00|starter_3k|Member|m@x.com|uid-1|starter_3k|||||||||S"
    assert payu_request_hash(**ARGS) == hashlib.sha512(seq.encode()).hexdigest()

def test_payu_response_hash_formula():
    seq = "S|success||||||||starter_3k|uid-1|m@x.com|Member|starter_3k|880.00|t1|K"
    assert payu_response_hash(status="success", **ARGS) == hashlib.sha512(seq.encode()).hexdigest()
```
Run → FAIL.

- [ ] **Step 2: Implement PayU half**

Append to `_pay.py`:

```python
import secrets as _secrets
from fastapi import Form
from fastapi.responses import RedirectResponse

def payu_request_hash(key: str, salt: str, txnid: str, amount: str, productinfo: str,
                      firstname: str, email: str, udf1: str, udf2: str) -> str:
    seq = f"{key}|{txnid}|{amount}|{productinfo}|{firstname}|{email}|{udf1}|{udf2}|||||||||{salt}"
    return hashlib.sha512(seq.encode()).hexdigest()

def payu_response_hash(key: str, salt: str, status: str, txnid: str, amount: str, productinfo: str,
                       firstname: str, email: str, udf1: str, udf2: str) -> str:
    seq = f"{salt}|{status}||||||||{udf2}|{udf1}|{email}|{firstname}|{productinfo}|{amount}|{txnid}|{key}"
    return hashlib.sha512(seq.encode()).hexdigest()

class PayUIn(BaseModel):
    pack: str

@router.post("/payu/initiate")
def payu_initiate(body: PayUIn, user: User = Depends(current_user)):
    if body.pack not in PACKS: raise HTTPException(400, "unknown pack")
    key, salt = os.getenv("PAYU_KEY", ""), os.getenv("PAYU_SALT", "")
    base = os.getenv("PAYU_BASE", "https://test.payu.in").rstrip("/")
    app_base = os.getenv("APP_BASE_URL", "").rstrip("/")
    if not key or not salt or not app_base: raise HTTPException(503, "PayU not configured")
    p = PACKS[body.pack]
    txnid = "ws" + _secrets.token_hex(10)
    amount = f"{p['amount_inr'] / 100:.2f}"
    fields = {"key": key, "txnid": txnid, "amount": amount, "productinfo": body.pack,
              "firstname": "Member", "email": user.email or "member@leadfinder.local",
              "udf1": user.id, "udf2": body.pack,
              "surl": f"{app_base}/api/pay/payu/return", "furl": f"{app_base}/api/pay/payu/return"}
    fields["hash"] = payu_request_hash(key, salt, txnid, amount, body.pack,
                                       fields["firstname"], fields["email"], user.id, body.pack)
    sb_post("orders", {"user_id": user.id, "pack": body.pack, "leads": p["leads"],
                       "amount_inr": p["amount_inr"], "gateway": "payu",
                       "gateway_order_id": txnid}, prefer="return=minimal")
    return {"action": f"{base}/_payment", "fields": fields}

@router.post("/payu/return")
def payu_return(status: str = Form(""), txnid: str = Form(""), amount: str = Form(""),
                productinfo: str = Form(""), firstname: str = Form(""), email: str = Form(""),
                udf1: str = Form(""), udf2: str = Form(""), hash: str = Form("")):
    key, salt = os.getenv("PAYU_KEY", ""), os.getenv("PAYU_SALT", "")
    expect = payu_response_hash(key, salt, status, txnid, amount, productinfo, firstname, email, udf1, udf2)
    ok = hmac.compare_digest(expect, hash) and status == "success"
    if ok:
        rows = sb_get("orders", {"gateway_order_id": f"eq.{txnid}", "select": "*"})
        if rows:
            credit_order(rows[0])
        else:
            ok = False
    else:
        sb_patch("orders", {"status": "failed"}, {"gateway_order_id": f"eq.{txnid}", "status": "neq.paid"})
    return RedirectResponse(url=f"/#billing?status={'ok' if ok else 'failed'}", status_code=303)
```

Add `python-multipart` to `vercel-app/requirements.txt`.

- [ ] **Step 3: Real `buyPayU`**

Replace the Task 10 stub:
```javascript
window.buyPayU = async pack => {
  const o = await authApi('/api/pay/payu/initiate', {method:'POST', body: JSON.stringify({pack})});
  const f = document.createElement('form'); f.method = 'POST'; f.action = o.action;
  for (const [k, v] of Object.entries(o.fields)) {
    const i = document.createElement('input'); i.type = 'hidden'; i.name = k; i.value = v; f.appendChild(i);
  }
  document.body.appendChild(f); f.submit();
};
```

- [ ] **Step 4: Tests + verify + commit**

`python -m pytest tests/cloud -v` → PASS (incl. both hash formula tests).
**Checkpoint — ask the user for PayU TEST key/salt.**
```bash
git add vercel-app/api/_pay.py vercel-app/requirements.txt vercel-app/index.html tests/cloud/test_pay.py
git commit -m "feat: PayU credit-pack checkout with reverse-hash verification"
```

---

### Task 12: Member AI keys in suggest + lead summaries

**Files:**
- Create: `vercel-app/api/_ai.py`
- Modify: `vercel-app/api/index.py` (mount `_ai`, delete old `/api/suggest`)
- Modify: `vercel-app/index.html` (summary button in lead rows)

**Interfaces:**
- Produces: `GET /api/suggest?q= -> {source, keywords}` (session auth; key order: member gemini → member openai → server `GEMINI_API_KEY` → Google autosuggest), `POST /api/leads/summarize {place_key} -> {summary}` (400 if no working key).
- Consumes: `user_settings` keys, `web_scraper_leads.ai_summary`.

- [ ] **Step 1: Implement `_ai.py`**

```python
"""AI endpoints running on the MEMBER's own keys."""
from __future__ import annotations
import json as _json
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from _auth import User, current_user
from _db import sb_get, sb_patch

router = APIRouter(prefix="/api")

def _keys(user_id: str) -> dict:
    rows = sb_get("user_settings", {"user_id": f"eq.{user_id}", "select": "gemini_key,openai_key"})
    return rows[0] if rows else {}

def _gemini(key: str, prompt: str) -> str | None:
    try:
        r = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={key}",
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"temperature": 0.4, "maxOutputTokens": 1000,
                                       "thinkingConfig": {"thinkingBudget": 0}}}, timeout=25)
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (httpx.HTTPError, KeyError, IndexError):
        return None

def _openai(key: str, prompt: str) -> str | None:
    try:
        r = httpx.post("https://api.openai.com/v1/chat/completions",
                       headers={"Authorization": f"Bearer {key}"},
                       json={"model": "gpt-4o-mini", "temperature": 0.4,
                             "messages": [{"role": "user", "content": prompt}]}, timeout=25)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError):
        return None

def _llm(user_id: str, prompt: str) -> str | None:
    k = _keys(user_id)
    if k.get("gemini_key"):
        out = _gemini(k["gemini_key"], prompt)
        if out: return out
    if k.get("openai_key"):
        out = _openai(k["openai_key"], prompt)
        if out: return out
    server_key = os.getenv("GEMINI_API_KEY")
    if server_key:
        return _gemini(server_key, prompt)
    return None

@router.get("/suggest")
def suggest(q: str, user: User = Depends(current_user)):
    q = (q or "").strip()
    if len(q) < 2: return {"source": "none", "keywords": []}
    prompt = (f"You help build Google Maps lead-generation searches. For the business type {q!r}, "
              "list 10 other Google Maps search keywords that find the same or closely related businesses. "
              "Reply as a JSON array of short strings only, no prose.")
    text = _llm(user.id, prompt)
    if text:
        try:
            kws = [str(x).strip() for x in _json.loads(text[text.find("["): text.rfind("]") + 1]) if str(x).strip()][:12]
            if kws: return {"source": "ai", "keywords": kws}
        except ValueError:
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
                    if s and s.lower() != q.lower() and s not in kws: kws.append(s)
    except (httpx.HTTPError, ValueError, IndexError):
        pass
    return {"source": "autosuggest", "keywords": kws[:12]}

class SummIn(BaseModel):
    place_key: str

@router.post("/leads/summarize")
def summarize(body: SummIn, user: User = Depends(current_user)):
    params = {"place_key": f"eq.{body.place_key}", "select": "*"}
    if user.role != "admin":
        params["user_id"] = f"eq.{user.id}"
    rows = sb_get("web_scraper_leads", params)
    if not rows: raise HTTPException(404, "no such lead")
    lead = rows[0]
    facts = {k: lead.get(k) for k in ("name", "category", "address", "phone", "email", "website",
                                      "instagram", "facebook", "rating", "reviews_count")}
    text = _llm(lead["user_id"], "Summarize this business as a sales lead in 2-3 sentences, "
                                 "mention outreach angle. Facts: " + _json.dumps(facts, ensure_ascii=False))
    if not text: raise HTTPException(400, "no working AI key — add one in Settings")
    sb_patch("web_scraper_leads", {"ai_summary": text.strip()},
             {"user_id": f"eq.{lead['user_id']}", "place_key": f"eq.{body.place_key}"})
    return {"summary": text.strip()}
```

Mount in `index.py`; delete the old `/api/suggest` there (keep `/api/geocode` — keyless).

- [ ] **Step 2: UI button**

In the leads-table renderer of `vercel-app/index.html` (the template building `#leads-tbl` rows), add per row: `<a style="cursor:pointer" title="AI summary" onclick="summLead('${r.place_key}')">🧠</a>`; add:

```javascript
window.summLead = async pk => {
  try { const r = await authApi('/api/leads/summarize', {method:'POST', body: JSON.stringify({place_key: pk})});
        alert(r.summary); loadLeads(true); }
  catch (e) { alert(e.message); }
};
```

- [ ] **Step 3: Verify + commit**

`python -m pytest tests/cloud -v` → PASS; import check clean; curl unauth `/api/suggest?q=cafe` → 401.
```bash
git add vercel-app/api vercel-app/index.html
git commit -m "feat: member-owned AI keys drive keyword suggest + lead summaries"
```

---

### Task 13: Deploy, env, smoke test, docs

**Files:**
- Modify: `CLAUDE.md`, `README.md`
- No code changes.

- [ ] **Step 1: Vercel env vars**

**Checkpoint — needs the user (or consent to run `npx vercel env`):** set on the `web-scraper-leads` project (Production): `SUPABASE_PROJECT_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_ANON_KEY`, `GEMINI_API_KEY` (server fallback), `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `PAYU_KEY`, `PAYU_SALT`, `PAYU_BASE=https://test.payu.in`, `APP_BASE_URL=https://web-scraper-leads.vercel.app`, `CRON_SECRET=<random>`.

- [ ] **Step 2: Deploy**

Run: `cd vercel-app && npx vercel deploy --prod` (`.vercel` link dir exists from the first deploy).

- [ ] **Step 3: Smoke test production**

```
curl -s https://web-scraper-leads.vercel.app/api/health   → {"ok":true,"cloud":true,...}
curl -s https://web-scraper-leads.vercel.app/api/config   → 2 packs
curl -s https://web-scraper-leads.vercel.app/api/jobs     → 401 (auth wall)
curl -s -X POST .../api/login -d '{"email":"<admin>","password":"<pass>"}' → tokens
```
Then ask the user to: sign in, create a member, create an agent token, run `python -m webscraper agent --token …`, create a small job, watch it complete, run one Razorpay test-mode purchase.

- [ ] **Step 4: Razorpay dashboard webhook**

**User action:** Razorpay dashboard → Webhooks → add `https://web-scraper-leads.vercel.app/api/pay/razorpay/webhook`, event `payment.captured`, secret = `RAZORPAY_WEBHOOK_SECRET`.

- [ ] **Step 5: Update docs**

`CLAUDE.md`: extend Layout with `agent.py`, `vercel-app/api/_*.py`, `supabase_migrations/`, `scripts/`; add "Cloud product" section: auth model (admin creates members), verified-lead rule, packs (3k/₹880, 5k/₹1,320), agent flow, env var list, "payment amounts are server-side constants in `_db.PACKS`". `README.md`: add "Run as a cloud member" section (login → Settings → agent token → `python -m webscraper agent --token …`).

- [ ] **Step 6: Final commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: cloud product architecture, agent usage, deploy env"
```

---

## Self-review notes (done at plan time)

- Spec coverage: auth/admin (T2–T4), isolation+RLS (T1, T8), job queue+agent (T6–T8), quota/ledger/RPC (T1, T8), webhooks (T9), Razorpay (T10), PayU (T11), AI keys (T5, T12), deploy+docs (T13). Stale-claim reclaim → T7; idempotent credit → T10 `credit_order`; test-webhook button → T5 UI + T9 endpoint.
- Deviation from spec: none functional. Vercel Hobby cron is daily-only (spec assumed 10-min) — noted in T9, optional n8n schedule flagged as a user decision.
- Type consistency: `User` dataclass shared; `sb_*` signatures uniform; `PACKS` single source; `enqueue_and_deliver(user_id, place_keys)` matches T8's call; `claim_is_stale(iso, now)` matches tests; timestamps always real ISO strings (never literal `"now()"` in PATCH bodies — T7 note).
