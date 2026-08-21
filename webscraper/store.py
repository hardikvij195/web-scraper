"""SQLite persistence: jobs + places. Stdlib sqlite3, one file under data/."""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from webscraper.config import settings
from webscraper.models import Place

PLACE_COLS = [
    "job_id", "place_key", "name", "category", "address", "country", "phone", "phone_digits", "website", "domain",
    "rating", "reviews_count", "price_range", "lat", "lng", "distance_km", "maps_url", "plus_code", "place_id",
    "email", "emails", "instagram", "facebook", "linkedin", "twitter_x", "youtube", "tiktok",
    "whatsapp_number", "whatsapp_source", "enrich_status", "enriched_at", "scraped_at",
    "summary", "owner", "team", "research_status", "researched_at", "raw",
]
_JSON_COLS = {"emails", "raw", "team"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  query TEXT NOT NULL,
  location TEXT,
  max_places INTEGER,
  delay_sec REAL,
  status TEXT NOT NULL DEFAULT 'running',   -- running | done | stopped | failed
  found_count INTEGER NOT NULL DEFAULT 0,
  note TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS places (
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  place_key TEXT NOT NULL,
  name TEXT, category TEXT, address TEXT, country TEXT, phone TEXT, phone_digits TEXT, website TEXT, domain TEXT,
  rating REAL, reviews_count INTEGER, price_range TEXT, lat REAL, lng REAL, distance_km REAL, maps_url TEXT, plus_code TEXT, place_id TEXT,
  email TEXT, emails TEXT, instagram TEXT, facebook TEXT, linkedin TEXT, twitter_x TEXT, youtube TEXT, tiktok TEXT,
  whatsapp_number TEXT, whatsapp_source TEXT,
  enrich_status TEXT NOT NULL DEFAULT 'pending', enriched_at TEXT, scraped_at TEXT,
  summary TEXT, owner TEXT, team TEXT, research_status TEXT, researched_at TEXT, raw TEXT,
  PRIMARY KEY (job_id, place_key)
);
CREATE INDEX IF NOT EXISTS idx_places_phone ON places(phone_digits);
CREATE INDEX IF NOT EXISTS idx_places_domain ON places(domain);
CREATE INDEX IF NOT EXISTS idx_places_enrich ON places(enrich_status);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_team(team: Any) -> str:
    """team list[dict] → 'Name (Role) — phone / email; …' for CSV/Excel cells."""
    if not isinstance(team, list):
        return ""
    out = []
    for t in team:
        if not isinstance(t, dict):
            continue
        bits = t.get("name") or ""
        if t.get("role"):
            bits += f" ({t['role']})"
        contact = " / ".join(x for x in (t.get("phone"), t.get("email")) if x)
        if contact:
            bits += f" — {contact}"
        if bits:
            out.append(bits)
    return "; ".join(out)


class Store:
    EXPORT_COLS = [
        "name", "category", "phone", "whatsapp_number", "whatsapp_source", "email", "emails", "website",
        "instagram", "facebook", "linkedin", "twitter_x", "youtube", "tiktok",
        "address", "country", "rating", "reviews_count", "price_range", "lat", "lng", "distance_km",
        "summary", "owner", "team", "maps_url", "place_id", "enrich_status", "job_id",
    ]

    def __init__(self, path: Path | None = None):
        self.path = path or settings.db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # timeout + WAL: the web server's request handlers and the scrape worker thread each
        # open their own Store; WAL lets readers poll while the worker writes.
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created (SQLite has no IF NOT EXISTS for ADD COLUMN)."""
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(places)")}
        for col, typ in (("price_range", "TEXT"), ("country", "TEXT"), ("distance_km", "REAL"),
                         ("summary", "TEXT"), ("owner", "TEXT"), ("team", "TEXT"),
                         ("research_status", "TEXT"), ("researched_at", "TEXT")):
            if col not in have:
                self.conn.execute(f"ALTER TABLE places ADD COLUMN {col} {typ}")
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        for col, typ in (
            ("phase", "TEXT"),            # queued | scraping | enriching | done | stopped | failed
            ("links_found", "INTEGER NOT NULL DEFAULT 0"),
            ("scraped_count", "INTEGER NOT NULL DEFAULT 0"),
            ("enrich_done", "INTEGER NOT NULL DEFAULT 0"),
            ("enrich_total", "INTEGER NOT NULL DEFAULT 0"),
            ("message", "TEXT"),
            ("stop_requested", "INTEGER NOT NULL DEFAULT 0"),
            ("do_enrich", "INTEGER NOT NULL DEFAULT 1"),
            ("headless", "INTEGER NOT NULL DEFAULT 1"),
            ("country", "TEXT"),
            ("reenrich_only", "INTEGER NOT NULL DEFAULT 0"),   # re-queued just to retry enrichment
            ("radius_km", "REAL"),           # optional: skip places farther than this from the centre
            ("center_lat", "REAL"), ("center_lng", "REAL"),   # resolved from Maps when the job starts
            ("window_start", "TEXT"), ("window_end", "TEXT"), # legacy clock window (API still honours it)
            ("max_minutes", "INTEGER"),      # stop scraping this many minutes after the job starts
            ("unique_new", "INTEGER NOT NULL DEFAULT 0"),      # skip places already scraped by ANY job
            ("skipped_known", "INTEGER NOT NULL DEFAULT 0"),
            ("do_research", "INTEGER NOT NULL DEFAULT 0"),     # LLM summary/owner/team pass
            ("research_done", "INTEGER NOT NULL DEFAULT 0"), ("research_total", "INTEGER NOT NULL DEFAULT 0"),
            ("skipped_far", "INTEGER NOT NULL DEFAULT 0"),
            ("started_at", "TEXT"),          # worker picked it up
            ("scrape_started_at", "TEXT"),
            ("enrich_started_at", "TEXT"),
        ):
            if col not in have:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
        # jobs created by the CLI before `phase` existed: derive it from status so the UI can
        # label them; a stale 'running' (crashed run) becomes 'stopped'.
        self.conn.execute(
            "UPDATE jobs SET phase = CASE status WHEN 'running' THEN 'stopped' ELSE status END WHERE phase IS NULL"
        )
        self.conn.commit()

    def update_job(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", [*fields.values(), job_id])
        self.conn.commit()

    def stop_requested(self, job_id: int) -> bool:
        r = self.conn.execute("SELECT stop_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
        return bool(r and r[0])

    def next_queued_job(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM jobs WHERE phase='queued' ORDER BY id LIMIT 1"
        ).fetchone()

    def queued_jobs(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM jobs WHERE phase IN ('queued','waiting') ORDER BY id").fetchall()

    def export_xlsx(self, job_id: int | None, out: Path | None = None, unique: bool = False) -> Path:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter

        rows = self.export_rows(job_id, unique)
        settings.export_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = out or settings.export_dir / (f"leads-job{job_id}-{stamp}.xlsx" if job_id else f"leads-all-{stamp}.xlsx")
        wb = Workbook()
        ws = wb.active
        ws.title = "Leads"
        ws.append(self.EXPORT_COLS)
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E79")
        for r in rows:
            r = dict(r)
            r["emails"] = "; ".join(r.get("emails") or []); r["team"] = fmt_team(r.get("team"))
            ws.append([r.get(c) for c in self.EXPORT_COLS])
        widths = {"name": 34, "address": 50, "website": 32, "email": 30, "emails": 36, "maps_url": 18,
                  "instagram": 30, "facebook": 30, "linkedin": 30, "twitter_x": 26, "youtube": 26}
        for i, c in enumerate(self.EXPORT_COLS, 1):
            ws.column_dimensions[get_column_letter(i)].width = widths.get(c, 16)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        wb.save(out)
        return out

    # ── jobs ────────────────────────────────────────────────────────────────
    def create_job(self, query: str, location: str | None, max_places: int, delay_sec: float,
                   phase: str = "scraping", do_enrich: bool = True, headless: bool = True,
                   country: str | None = None, radius_km: float | None = None,
                   window_start: str | None = None, window_end: str | None = None,
                   center_lat: float | None = None, center_lng: float | None = None,
                   max_minutes: int | None = None, unique_new: bool = False) -> int:
        cur = self.conn.execute(
            "INSERT INTO jobs(query, location, max_places, delay_sec, status, created_at, phase, do_enrich, headless, "
            "country, radius_km, window_start, window_end, center_lat, center_lng, max_minutes, unique_new) "
            "VALUES (?,?,?,?,'running',?,?,?,?,?,?,?,?,?,?,?,?)",
            (query, location, max_places, delay_sec, now_iso(), phase, int(do_enrich), int(headless), country,
             radius_km, window_start, window_end, center_lat, center_lng, max_minutes, int(unique_new)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_job(self, job_id: int, status: str = "done", note: str | None = None) -> None:
        n = self.conn.execute("SELECT COUNT(*) FROM places WHERE job_id=?", (job_id,)).fetchone()[0]
        self.conn.execute(
            "UPDATE jobs SET status=?, note=?, found_count=?, finished_at=? WHERE id=?",
            (status, note, n, now_iso(), job_id),
        )
        self.conn.commit()

    def get_job(self, job_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def list_jobs(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT j.*, (SELECT COUNT(*) FROM places p WHERE p.job_id=j.id) AS places FROM jobs j ORDER BY id DESC"
        ).fetchall()

    # ── places ──────────────────────────────────────────────────────────────
    def known_place_keys(self, job_id: int) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT place_key FROM places WHERE job_id=?", (job_id,))}

    def all_place_keys(self) -> set[str]:
        """Every place ever scraped, across all jobs — used by 'only new businesses' jobs."""
        return {r[0] for r in self.conn.execute("SELECT DISTINCT place_key FROM places")}

    def leads_all(self, unique: bool = True) -> list[dict[str, Any]]:
        """All scraped leads across jobs, newest first, joined with their job's query/location.
        `unique` keeps the most recently scraped row per place."""
        q = ("SELECT p.*, j.query AS job_query, j.location AS job_location FROM places p "
             "JOIN jobs j ON j.id = p.job_id ORDER BY p.scraped_at DESC, p.rowid DESC")
        rows = [dict(r) for r in self.conn.execute(q)]
        for r in rows:
            for c in _JSON_COLS:
                try:
                    r[c] = json.loads(r[c]) if r[c] else ({} if c == "raw" else [])
                except (TypeError, json.JSONDecodeError):
                    r[c] = {} if c == "raw" else []
        if not unique:
            return rows
        seen: set[str] = set()
        out = []
        for r in rows:            # newest first → first occurrence wins
            if r["place_key"] in seen:
                continue
            seen.add(r["place_key"])
            out.append(r)
        return out

    def upsert_place(self, p: Place) -> None:
        row = p.to_row()
        vals = [json.dumps(row[c], ensure_ascii=False) if c in _JSON_COLS else row[c] for c in PLACE_COLS]
        placeholders = ",".join("?" for _ in PLACE_COLS)
        updates = ",".join(f"{c}=excluded.{c}" for c in PLACE_COLS if c not in ("job_id", "place_key"))
        self.conn.execute(
            f"INSERT INTO places({','.join(PLACE_COLS)}) VALUES ({placeholders}) "
            f"ON CONFLICT(job_id, place_key) DO UPDATE SET {updates}",
            vals,
        )
        self.conn.commit()

    def update_enrichment(self, job_id: int, place_key: str, fields: dict[str, Any]) -> None:
        fields = dict(fields)
        if "emails" in fields and not isinstance(fields["emails"], str):
            fields["emails"] = json.dumps(fields["emails"], ensure_ascii=False)
        cols = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE places SET {cols} WHERE job_id=? AND place_key=?",
            [*fields.values(), job_id, place_key],
        )
        self.conn.commit()

    def places(self, job_id: int | None = None, enrich_status: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM places WHERE 1=1"
        args: list[Any] = []
        if job_id is not None:
            q += " AND job_id=?"
            args.append(job_id)
        if enrich_status is not None:
            q += " AND enrich_status=?"
            args.append(enrich_status)
        q += " ORDER BY job_id, rowid"
        rows = [dict(r) for r in self.conn.execute(q, args)]
        for r in rows:
            for c in _JSON_COLS:
                try:
                    r[c] = json.loads(r[c]) if r[c] else ({} if c == "raw" else [])
                except (TypeError, json.JSONDecodeError):
                    r[c] = {} if c == "raw" else []
        return rows

    def stats(self) -> dict[str, Any]:
        c = self.conn
        total = c.execute("SELECT COUNT(*) FROM places").fetchone()[0]

        def cnt(where: str) -> int:
            return c.execute(f"SELECT COUNT(*) FROM places WHERE {where}").fetchone()[0]

        return {
            "jobs": c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            "places": total,
            "with_phone": cnt("phone IS NOT NULL AND phone <> ''"),
            "with_website": cnt("website IS NOT NULL AND website <> ''"),
            "with_email": cnt("email IS NOT NULL AND email <> ''"),
            "with_instagram": cnt("instagram IS NOT NULL"),
            "with_facebook": cnt("facebook IS NOT NULL"),
            "with_linkedin": cnt("linkedin IS NOT NULL"),
            "with_twitter_x": cnt("twitter_x IS NOT NULL"),
            "wa_maps_link": cnt("whatsapp_source='maps_link'"),
            "wa_link": cnt("whatsapp_source='wa_link'"),
            "wa_assumed_mobile": cnt("whatsapp_source='assumed_mobile'"),
            "enrich_pending": cnt("enrich_status='pending'"),
            "enrich_done": cnt("enrich_status='done'"),
            "enrich_thin": cnt("enrich_status='thin'"),
            "enrich_failed": cnt("enrich_status='failed'"),
            "enrich_no_website": cnt("enrich_status='no_website'"),
            "unique_phones": c.execute("SELECT COUNT(DISTINCT phone_digits) FROM places WHERE phone_digits IS NOT NULL").fetchone()[0],
        }

    # ── export ──────────────────────────────────────────────────────────────
    def export_rows(self, job_id: int | None, unique: bool = False) -> list[dict[str, Any]]:
        """Rows for export. `unique` keeps the first place per website domain — collapses
        chains (one business, many branches) into a single lead; rows without a website stay."""
        rows = self.places(job_id)
        if not unique:
            return rows
        seen: set[str] = set()
        out = []
        for r in rows:
            d = r.get("domain")
            if d:
                if d in seen:
                    continue
                seen.add(d)
            out.append(r)
        return out

    def export_leads(self, fmt: str, unique: bool = True) -> Path:
        """Export the cross-job leads list (place-level unique)."""
        rows = self.leads_all(unique)
        cols = self.EXPORT_COLS + ["scraped_at", "job_query", "job_location"]
        settings.export_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = settings.export_dir / f"all-leads-{stamp}.{fmt}"
        if fmt == "json":
            out.write_text(json.dumps([{c: r.get(c) for c in cols} for r in rows],
                                      ensure_ascii=False, indent=2), encoding="utf-8")
        elif fmt == "csv":
            with out.open("w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    r = dict(r)
                    r["emails"] = "; ".join(r.get("emails") or []); r["team"] = fmt_team(r.get("team"))
                    w.writerow({c: r.get(c) for c in cols})
        else:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill
            from openpyxl.utils import get_column_letter
            wb = Workbook()
            ws = wb.active
            ws.title = "All leads"
            ws.append(cols)
            for cell in ws[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="1F4E79")
            for r in rows:
                r = dict(r)
                r["emails"] = "; ".join(r.get("emails") or []); r["team"] = fmt_team(r.get("team"))
                ws.append([r.get(c) for c in cols])
            for i, c in enumerate(cols, 1):
                ws.column_dimensions[get_column_letter(i)].width = {"name": 34, "address": 50, "website": 32,
                    "email": 30, "emails": 36, "instagram": 30, "facebook": 30, "linkedin": 30,
                    "scraped_at": 20, "job_query": 20, "job_location": 20}.get(c, 16)
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            wb.save(out)
        return out

    def export(self, job_id: int | None, fmt: str, out: Path | None = None, unique: bool = False) -> Path:
        rows = self.export_rows(job_id, unique)
        settings.export_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"leads-job{job_id}-{stamp}" if job_id else f"leads-all-{stamp}"
        out = out or settings.export_dir / f"{name}.{fmt}"
        if fmt == "json":
            out.write_text(json.dumps([{c: r.get(c) for c in self.EXPORT_COLS} for r in rows],
                                      ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            with out.open("w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=self.EXPORT_COLS, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    r = dict(r)
                    r["emails"] = "; ".join(r.get("emails") or []); r["team"] = fmt_team(r.get("team"))
                    w.writerow({c: r.get(c) for c in self.EXPORT_COLS})
        return out

    def close(self) -> None:
        self.conn.close()
