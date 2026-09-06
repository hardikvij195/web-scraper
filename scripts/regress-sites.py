#!/usr/bin/env python
"""Regression run over the curated site list (W56 / CRM T398).

    python scripts/regress-sites.py                      # docs/test-sites.md, every section
    python scripts/regress-sites.py --section cloudflare # one section (substring match)
    python scripts/regress-sites.py --csv docs/test-sites.csv --class http_403 --limit 30
    python scripts/regress-sites.py --headed --json out.json

Runs the REAL enrichment ladder (`enrich.crawl_site`: httpx → curl_cffi → real Chrome) over
each listed domain and prints, per site, the tier that read it and what was extracted, then
pass/fail totals per section. "Pass" = the site's expectation in the list is met:
  reachable  the home page was read by any tier
  email      at least one email was extracted
  social     at least one social profile was found
  any        email OR social OR phone
  dead       the site is expected to stay unreadable (dns / gone) — a pass is a FAIL here,
             because it means the list is stale, not that the scraper improved

Re-run this after every scraper change; compare the totals with the previous run's `--json`.
No CRM, no SQLite — only the network and the ladder. Needs Chrome for the browser tier
(skipped with a note when it cannot start)."""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from webscraper.enrich import HEADERS, crawl_site  # noqa: E402

ROW_RE = re.compile(r"^\|\s*`?([a-z0-9.\-]+\.[a-z]{2,})`?\s*\|\s*([^|]*)\|\s*([^|]*)\|\s*([^|]*)\|")
EXPECT = ("reachable", "email", "social", "any", "dead")


def parse_md(path: Path, section: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cur = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            cur = line[3:].strip()
            continue
        m = ROW_RE.match(line)
        if not m or m.group(1) == "domain":
            continue
        if section and section.lower() not in cur.lower():
            continue
        exp = m.group(3).strip().lower()
        out.append({"domain": m.group(1), "section": cur, "why": m.group(2).strip(),
                    "expect": exp if exp in EXPECT else "reachable", "last": m.group(4).strip()})
    return out


def parse_csv(path: Path, klass: str | None, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if klass and r.get("blocker_class") != klass:
                continue
            bc = r.get("blocker_class") or ""
            out.append({"domain": r["domain"], "section": bc or "all", "why": r.get("enrich_error") or "",
                        "expect": "dead" if bc in ("dns", "gone") else "reachable", "last": r.get("tier_that_worked") or ""})
            if limit and len(out) >= limit:
                break
    return out


async def run(sites: list[dict[str, Any]], headed: bool, concurrency: int) -> None:
    browser: dict[str, Any] = {"fetcher": None, "off": False}
    lock = asyncio.Lock()

    async def browser_retry(url: str) -> tuple[str | None, str | None, str | None]:
        async with lock:
            if browser["fetcher"] is None and not browser["off"]:
                try:
                    from webscraper.browser_fetch import BrowserFetcher
                    browser["fetcher"] = await asyncio.to_thread(lambda: BrowserFetcher(headless=not headed))
                except Exception as e:  # noqa: BLE001
                    print(f"  ! browser tier unavailable: {e}")
                    browser["off"] = True
        if browser["fetcher"] is None:
            return None, None, None
        html, err = await asyncio.to_thread(browser["fetcher"].fetch_ex, url)
        return html, None, err

    sem = asyncio.Semaphore(concurrency)
    timeout = httpx.Timeout(15.0, connect=10.0)
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=timeout, verify=False) as client:
        async def one(s: dict[str, Any]) -> None:
            async with sem:
                t0 = time.time()
                try:
                    c, reason = await crawl_site(client, s["domain"], browser_retry, None, region="GB")
                except Exception as e:  # noqa: BLE001
                    c, reason = None, f"exception: {e}"
                s["secs"] = round(time.time() - t0, 1)
                if c is None or c.pages_fetched == 0:
                    s["via"], s["error"], s["emails"], s["socials"], s["phones"] = None, reason, 0, 0, 0
                else:
                    s["via"], s["error"], s["emails"] = c.via, None, len(c.emails)
                    s["socials"] = sum(1 for k in ("instagram", "facebook", "linkedin", "twitter_x", "youtube", "tiktok") if getattr(c, k))
                    s["phones"] = len(c.phones) + (1 if c.whatsapp_number else 0)
                s["pass"] = judge(s)
                mark = "PASS" if s["pass"] else "FAIL"
                got = s["via"] or s["error"]
                print(f"  {mark:4} {s['domain']:42} {str(got):22} e={s['emails']} s={s['socials']} p={s['phones']} {s['secs']}s")
        try:
            await asyncio.gather(*(one(s) for s in sites))
        finally:
            if browser["fetcher"] is not None:
                await asyncio.to_thread(browser["fetcher"].close)


def judge(s: dict[str, Any]) -> bool:
    read = s["via"] is not None
    e = s["expect"]
    if e == "dead":
        return not read
    if e == "reachable":
        return read
    if e == "email":
        return s["emails"] > 0
    if e == "social":
        return s["socials"] > 0
    return read and (s["emails"] > 0 or s["socials"] > 0 or s["phones"] > 0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", default=str(ROOT / "docs" / "test-sites.md"))
    ap.add_argument("--csv")
    ap.add_argument("--section")
    ap.add_argument("--class", dest="klass")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--json")
    a = ap.parse_args()
    sites = parse_csv(Path(a.csv), a.klass, a.limit) if a.csv else parse_md(Path(a.md), a.section)
    if a.limit and not a.csv:
        sites = sites[:a.limit]
    if not sites:
        print("no sites matched"); sys.exit(2)
    print(f"{len(sites)} sites · headed={a.headed}")
    asyncio.run(run(sites, a.headed, a.concurrency))
    print()
    sections = sorted({s["section"] for s in sites}, key=lambda k: [s["section"] for s in sites].index(k))
    tot_pass = 0
    for sec in sections:
        rows = [s for s in sites if s["section"] == sec]
        p = sum(1 for s in rows if s["pass"])
        tot_pass += p
        tiers = {}
        for s in rows:
            tiers[s["via"] or "-"] = tiers.get(s["via"] or "-", 0) + 1
        print(f"{sec:60} {p:3}/{len(rows):<3}  {tiers}")
    print(f"{'TOTAL':60} {tot_pass:3}/{len(sites)}")
    if a.json:
        Path(a.json).write_text(json.dumps(sites, indent=1), encoding="utf-8")
        print("written", a.json)


if __name__ == "__main__":
    main()
