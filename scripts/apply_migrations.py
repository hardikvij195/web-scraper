"""Apply supabase_migrations/*.sql in order over direct Postgres.
Usage: python scripts/apply_migrations.py [001 002 003]
Needs SUPABASE_PROJECT_URL and SUPABASE_DB_PASS in env or .env / vercel-app/.env.deploy.
Fallback if no DB pass: paste each file into the Supabase SQL editor by hand.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_env(p: Path) -> None:
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    load_env(ROOT / ".env")
    load_env(ROOT / "vercel-app" / ".env.deploy")
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
        except Exception:  # noqa: BLE001 — wrong region / tenant not found; try the next
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
