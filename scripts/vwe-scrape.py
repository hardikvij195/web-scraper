"""T561 — VALVE WORLD EXPO 2026 exhibitor directory → Excel.

    python scripts/vwe-scrape.py [--out data/exports/valveworld2026-exhibitors.xlsx] [--no-enrich] [--limit N]

Source: the Messe Düsseldorf "VIS" JSON API behind https://www.valveworldexpo.com/vis/v1/en/directory/<letter>.
Every call needs the header `x-vis-domain: www.valveworldexpo.com`. Per letter (a–z + `oth`)
the directory returns the exhibitors; per exhibitor the `slices/profile`, `slices/contacts`
and `slices/products` endpoints return company data, contact persons and products.
What the expo profile lacks (socials, more emails, more phones) is read from the company
website with the scraper's own crawler (`webscraper.enrich.crawl_site`, httpx tier only).

Sheets: Exhibitors (one row per company), Contacts (one row per person), Products, Categories.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import string
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from webscraper.enrich import HEADERS as SITE_HEADERS, crawl_site  # noqa: E402

BASE = "https://www.valveworldexpo.com/vis-api/vis/v1/en"
SITE = "https://www.valveworldexpo.com"
API_HEADERS = {
    "x-vis-domain": "www.valveworldexpo.com",
    "accept": "application/json",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
    "referer": SITE + "/vis/v1/en/directory/a",
}
LETTERS = list(string.ascii_lowercase) + ["other"]   # "other" = the 0-9 list
log = logging.getLogger("vwe")


async def get_json(client: httpx.AsyncClient, path: str, tries: int = 3) -> Any:
    for i in range(tries):
        try:
            r = await client.get(BASE + path, headers=API_HEADERS, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (404, 406):
                return None
            log.warning("%s → %s (try %d)", path, r.status_code, i + 1)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("%s → %s (try %d)", path, e, i + 1)
        await asyncio.sleep(1.5 * (i + 1))
    return None


def social_map(items: list[dict] | None) -> dict[str, str]:
    """VIS socialMedia entries → {linkedin, facebook, instagram, twitter_x, youtube, tiktok, xing, other}."""
    out: dict[str, str] = {}
    for it in items or []:
        url = (it.get("link") or it.get("url") or it.get("href") or "").strip()
        kind = (it.get("type") or it.get("platform") or it.get("name") or "").lower()
        if not url:
            continue
        u = url.lower()
        key = ("linkedin" if "linkedin" in u or "linkedin" in kind else "facebook" if "facebook" in u or "facebook" in kind
               else "instagram" if "instagram" in u or "instagram" in kind else "twitter_x" if "twitter" in u or "x.com" in u or "twitter" in kind
               else "youtube" if "youtube" in u or "youtu.be" in u else "tiktok" if "tiktok" in u else "xing" if "xing" in u else "other")
        out.setdefault(key, url)
    return out


def person_row(exh: dict, c: dict) -> dict:
    name = " ".join(x for x in [c.get("title"), c.get("firstName") or c.get("firstname"), c.get("lastName") or c.get("lastname")] if x) or c.get("name") or ""
    soc = social_map(c.get("socialMedia") or c.get("socials"))
    return {
        "company": exh["name"], "person": name.strip(), "position": c.get("position") or c.get("jobTitle") or c.get("function") or "",
        "email": c.get("email") or "", "phone": (c.get("phone") or {}).get("phone", "") if isinstance(c.get("phone"), dict) else (c.get("phone") or ""),
        "mobile": c.get("mobile") or "", "linkedin": soc.get("linkedin", ""), "xing": soc.get("xing", ""), "other_social": soc.get("other", ""),
        "languages": ", ".join(str(x.get("label") or x.get("id") or x) if isinstance(x, dict) else str(x) for x in (c.get("languages") or []) if x) if isinstance(c.get("languages"), list) else "",
        "profile_url": exh["profile_url"],
    }


async def fetch_exhibitor(client: httpx.AsyncClient, sem: asyncio.Semaphore, item: dict) -> dict:
    exh = item["exh"]
    async with sem:
        profile, contacts, products = await asyncio.gather(
            get_json(client, f"/exhibitors/{exh}/slices/profile"),
            get_json(client, f"/exhibitors/{exh}/slices/contacts?parentModule=profile&parentModule=stand"),
            get_json(client, f"/exhibitors/{exh}/slices/products"),
        )
        await asyncio.sleep(0.25)
    p = profile or {}
    addr = p.get("profileAddress") or {}
    links = [l.get("link") for l in (p.get("links") or []) if isinstance(l, dict) and l.get("link")]
    soc = social_map(p.get("socialMedia"))
    row = {
        "name": p.get("name") or item.get("name") or item.get("exhName") or "",
        "email": p.get("email") or p.get("getInTouchEmail") or "",
        "phone": (p.get("phone") or {}).get("phone", "") if isinstance(p.get("phone"), dict) else "",
        "website": links[0] if links else "",
        "other_links": " | ".join(str(l) for l in links[1:] if l),
        "address": ", ".join(str(a) for a in (addr.get("address") or []) if a), "zip": addr.get("zip") or "", "city": addr.get("city") or item.get("city") or "",
        "state": addr.get("state") or "", "country": addr.get("country") or item.get("country") or "", "country_code": addr.get("countryCode") or "",
        "hall_stand": p.get("location") or item.get("location") or "",
        "categories": " | ".join(c.get("label", "") for c in (p.get("categories") or []) if isinstance(c, dict)),
        "tags": " | ".join(str(t.get("label", t) if isinstance(t, dict) else t) for t in (p.get("tags") or [])),
        "linkedin": soc.get("linkedin", ""), "facebook": soc.get("facebook", ""), "instagram": soc.get("instagram", ""),
        "twitter_x": soc.get("twitter_x", ""), "youtube": soc.get("youtube", ""), "xing": soc.get("xing", ""),
        "premium": bool(p.get("premium") or item.get("premium")),
        "products": " | ".join(str(x.get("name") or x.get("title") or "") for x in (products or []) if isinstance(x, dict)),
        "profile_url": f"{SITE}/vis/v1/en/exhprofiles/{item.get('exhSeoId')}",
        "details_url": f"{SITE}/vis/v1/en/exhprofiles/{item.get('exhSeoId')}/details",
        "exh_id": exh,
        "_contacts": contacts or [],
        "_products": products or [],
    }
    return row


async def enrich_site(client: httpx.AsyncClient, sem: asyncio.Semaphore, row: dict) -> None:
    """Fill emails / socials / phones from the company website (httpx tier only — fast)."""
    site = row.get("website")
    if not site:
        return
    async with sem:
        try:
            contacts, reason = await asyncio.wait_for(crawl_site(client, site, region=row.get("country_code") or None), timeout=60)
        except Exception as e:  # noqa: BLE001
            row["site_error"] = str(e)[:80]
            return
    row["site_error"] = reason or ""
    emails = [e for e in contacts.emails if e]
    if emails:
        row["site_emails"] = ", ".join(dict.fromkeys(emails))
        if not row["email"]:
            row["email"] = emails[0]
    for k in ("linkedin", "facebook", "instagram", "twitter_x", "youtube"):
        v = getattr(contacts, k, None)
        if v and not row.get(k):
            row[k] = v
    if contacts.phones:
        row["site_phones"] = ", ".join(dict.fromkeys(contacts.phones))
    if contacts.whatsapp_number:
        row["whatsapp"] = contacts.whatsapp_number


def write_xlsx(path: Path, rows: list[dict]) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    cols = ["name", "email", "site_emails", "phone", "site_phones", "whatsapp", "website", "other_links", "linkedin", "facebook", "instagram",
            "twitter_x", "youtube", "xing", "contact_persons", "owner_or_main_contact", "address", "zip", "city", "state", "country", "country_code",
            "hall_stand", "categories", "tags", "products", "premium", "profile_url", "details_url", "site_error", "exh_id"]
    ws = wb.active
    ws.title = "Exhibitors"
    ws.append(cols)
    for r in rows:
        persons = [person_row(r, c) for c in r["_contacts"] if isinstance(c, dict)]
        r["contact_persons"] = " | ".join(f"{p['person']} ({p['position']})" if p["position"] else p["person"] for p in persons if p["person"])
        r["owner_or_main_contact"] = next((p["person"] for p in persons if re.search(r"owner|ceo|managing|director|president|founder|geschäftsführ|inhaber|gm\b", p["position"], re.I)), persons[0]["person"] if persons else "")
        ws.append([r.get(c, "") for c in cols])
    ws2 = wb.create_sheet("Contacts")
    pcols = ["company", "person", "position", "email", "phone", "mobile", "linkedin", "xing", "other_social", "languages", "profile_url"]
    ws2.append(pcols)
    for r in rows:
        for c in r["_contacts"]:
            if isinstance(c, dict):
                p = person_row(r, c)
                ws2.append([p.get(k, "") for k in pcols])
    ws3 = wb.create_sheet("Products")
    ws3.append(["company", "product", "description", "categories", "profile_url"])
    for r in rows:
        for x in r["_products"]:
            if isinstance(x, dict):
                ws3.append([r["name"], x.get("name") or x.get("title") or "", (x.get("description") or x.get("text") or "")[:500],
                            " | ".join(c.get("label", "") for c in (x.get("categories") or []) if isinstance(c, dict)), r["profile_url"]])
    ws4 = wb.create_sheet("Categories")
    ws4.append(["category", "exhibitors"])
    counts: dict[str, int] = {}
    for r in rows:
        for c in filter(None, r["categories"].split(" | ")):
            counts[c] = counts.get(c, 0) + 1
    for c, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        ws4.append([c, n])
    for w in (ws, ws2, ws3, ws4):
        for cell in w[1]:
            cell.font = Font(bold=True)
        w.freeze_panes = "A2"
        for i, col in enumerate(w.iter_cols(min_row=1, max_row=min(w.max_row, 200)), start=1):
            width = max((len(str(c.value)) if c.value is not None else 0) for c in col)
            w.column_dimensions[get_column_letter(i)].width = min(max(10, width + 2), 60)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/exports/valveworld2026-exhibitors.xlsx")
    ap.add_argument("--no-enrich", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    t0 = time.time()

    async with httpx.AsyncClient(follow_redirects=True) as client:
        items: list[dict] = []
        seen: set[str] = set()
        for letter in LETTERS:
            data = await get_json(client, f"/directory/{letter}")
            n = 0
            for it in data or []:
                if isinstance(it, dict) and it.get("exh") and it["exh"] not in seen:
                    seen.add(it["exh"]); items.append(it); n += 1
            print(f"letter {letter}: {n} exhibitors", flush=True)
        print(f"total exhibitors: {len(items)}", flush=True)
        if args.limit:
            items = items[: args.limit]
        sem = asyncio.Semaphore(args.concurrency)
        rows = await asyncio.gather(*(fetch_exhibitor(client, sem, it) for it in items))
        print(f"profiles fetched: {len(rows)} in {time.time() - t0:.0f}s · with website {sum(1 for r in rows if r['website'])} · with email {sum(1 for r in rows if r['email'])} · with contacts {sum(1 for r in rows if r['_contacts'])}", flush=True)

    if not args.no_enrich:
        t1 = time.time()
        async with httpx.AsyncClient(headers=SITE_HEADERS, follow_redirects=True, timeout=20) as sclient:
            sem2 = asyncio.Semaphore(8)
            await asyncio.gather(*(enrich_site(sclient, sem2, r) for r in rows))
        print(f"websites crawled in {time.time() - t1:.0f}s · emails now {sum(1 for r in rows if r['email'])} · linkedin {sum(1 for r in rows if r.get('linkedin'))} · facebook {sum(1 for r in rows if r.get('facebook'))} · instagram {sum(1 for r in rows if r.get('instagram'))}", flush=True)

    out = Path(args.out)
    write_xlsx(out, rows)
    json.dump([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows], open(out.with_suffix(".json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"wrote {out} ({len(rows)} exhibitors) in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    asyncio.run(main())
