"""Optional Supabase sync over PostgREST (no extra deps — plain httpx). When SUPABASE_PROJECT_URL
+ a service/secret key are in .env, scraped leads are pushed to a `web_scraper_leads` table and
the All-leads tab can load from there so data survives across machines.

The table can't be created over PostgREST, so `status()` returns the one-time CREATE SQL to paste
into the Supabase SQL editor; everything else (upsert/select) works once it exists.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from webscraper.store import Store, fmt_team

log = logging.getLogger("webscraper.supa")
TABLE = "web_scraper_leads"

SETUP_SQL = f"""create table if not exists public.{TABLE} (
  place_key text primary key,
  name text, category text, phone text, whatsapp_number text, whatsapp_source text,
  email text, emails text, website text,
  instagram text, facebook text, linkedin text, twitter_x text, youtube text, tiktok text,
  address text, country text, rating numeric, reviews_count int, price_range text,
  lat numeric, lng numeric, summary text, owner text, team text,
  maps_url text, place_id text, enrich_status text,
  scraped_at timestamptz, job_id int, job_query text, job_location text,
  synced_at timestamptz default now()
);
alter table public.{TABLE} enable row level security;
create policy "service role full access" on public.{TABLE}
  for all to service_role using (true) with check (true);"""

# columns pushed to Supabase (subset of place row, flattened)
_COLS = ["place_key", "name", "category", "phone", "whatsapp_number", "whatsapp_source",
         "wa_verified", "email", "emails", "website", "instagram", "facebook", "linkedin", "twitter_x",
         "youtube", "tiktok", "address", "country", "rating", "reviews_count", "price_range",
         "lat", "lng", "summary", "owner", "team", "maps_url", "place_id", "enrich_status",
         # WHY a crawl found nothing (http_403 | dns | timeout | non_html | no_pages).
         # Without it the CRM can show THAT enrichment failed but never why — the state
         # that made job #6's 9 WAF-blocked leads look arbitrarily broken.
         # ⚠ The SaaS `web_scraper_leads` table lacks this column AND `wa_verified` (no
         # migration ever added them, see W11), so that push already 400s. The CRM's
         # `lead_gen_results`, which is the path actually in use, has both.
         "enrich_error",
         "scraped_at", "job_id", "job_query", "job_location"]


def _cfg() -> tuple[str, str] | None:
    url = (os.getenv("SUPABASE_PROJECT_URL") or "").strip().rstrip("/")
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SECRET_KEY") or "").strip()
    return (url, key) if url and key else None


def enabled() -> bool:
    return _cfg() is not None


def _headers(key: str, extra: dict | None = None) -> dict:
    h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def _row(r: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for c in _COLS:
        v = r.get(c)
        if c == "emails":
            v = "; ".join(r.get("emails") or []) if isinstance(r.get("emails"), list) else v
        elif c == "team":
            v = fmt_team(r.get("team"))
        out[c] = v
    return out


def status() -> dict[str, Any]:
    cfg = _cfg()
    if not cfg:
        return {"configured": False, "reachable": False, "table_exists": False,
                "count": 0, "setup_sql": SETUP_SQL}
    url, key = cfg
    try:
        r = httpx.get(f"{url}/rest/v1/{TABLE}?select=place_key",
                      headers=_headers(key, {"Prefer": "count=exact", "Range": "0-0"}), timeout=15)
        if r.status_code == 404:
            return {"configured": True, "reachable": True, "table_exists": False,
                    "count": 0, "setup_sql": SETUP_SQL}
        r.raise_for_status()
        cr = r.headers.get("content-range", "*/0")
        count = int(cr.split("/")[-1]) if "/" in cr and cr.split("/")[-1].isdigit() else 0
        return {"configured": True, "reachable": True, "table_exists": True, "count": count, "setup_sql": SETUP_SQL}
    except httpx.HTTPError as e:
        log.warning("supabase status failed: %s", e)
        return {"configured": True, "reachable": False, "table_exists": False,
                "count": 0, "setup_sql": SETUP_SQL, "error": str(e)[:200]}


def create_table_via_pg() -> bool:
    """Create the table over direct Postgres (PostgREST can't do DDL). Needs SUPABASE_DB_PASS and
    psycopg2. Tries the session pooler across regions (the project's region isn't in any env var).
    Returns True on success."""
    cfg = _cfg()
    pw = (os.getenv("SUPABASE_DB_PASS") or "").strip()
    if not cfg or not pw:
        return False
    url, _ = cfg
    ref = url.split("//")[-1].split(".")[0]
    try:
        import psycopg2
    except ImportError:
        log.warning("psycopg2 not installed — cannot auto-create the Supabase table")
        return False
    regions = ["ap-northeast-1", "ap-southeast-1", "ap-south-1", "us-east-1", "us-east-2",
               "us-west-1", "us-west-2", "eu-west-1", "eu-west-2", "eu-central-1",
               "ap-southeast-2", "ap-northeast-2", "sa-east-1", "ca-central-1"]
    for reg in regions:
        try:
            c = psycopg2.connect(host=f"aws-0-{reg}.pooler.supabase.com", port=5432,
                                 user=f"postgres.{ref}", password=pw, dbname="postgres",
                                 connect_timeout=6, sslmode="require")
        except Exception as e:  # noqa: BLE001
            if "ENOTFOUND" in str(e) or "Tenant or user not found" in str(e):
                continue
            log.warning("supabase PG connect (%s): %s", reg, str(e)[:120])
            continue
        try:
            c.autocommit = True
            c.cursor().execute(SETUP_SQL)
            log.info("created Supabase table via pooler region %s", reg)
            return True
        finally:
            c.close()
    log.warning("could not reach the project's Postgres pooler in any region")
    return False


def push_rows(rows: list[dict[str, Any]]) -> int:
    """Upsert flattened place rows (conflict on place_key). Best-effort; returns count pushed."""
    cfg = _cfg()
    if not cfg or not rows:
        return 0
    url, key = cfg
    payload = [_row(r) for r in rows if r.get("place_key")]
    pushed = 0
    with httpx.Client(timeout=30) as c:
        for i in range(0, len(payload), 500):     # chunk to keep requests small
            batch = payload[i:i + 500]
            try:
                r = c.post(f"{url}/rest/v1/{TABLE}?on_conflict=place_key",
                           headers=_headers(key, {"Prefer": "resolution=merge-duplicates,return=minimal"}),
                           json=batch)
                if r.status_code >= 300:
                    log.warning("supabase push %s: %s", r.status_code, r.text[:200])
                    break
                pushed += len(batch)
            except httpx.HTTPError as e:
                log.warning("supabase push failed: %s", e)
                break
    return pushed


def push_job(store: Store, job_id: int) -> int:
    return push_rows(store.places(job_id))


def fetch_leads(limit: int = 5000) -> list[dict[str, Any]]:
    cfg = _cfg()
    if not cfg:
        return []
    url, key = cfg
    try:
        r = httpx.get(f"{url}/rest/v1/{TABLE}?select=*&order=scraped_at.desc&limit={limit}",
                      headers=_headers(key), timeout=30)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        log.warning("supabase fetch failed: %s", e)
        return []
