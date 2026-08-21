"""Optional deep-research pass over each lead's website using Gemini: a one-line business
summary, the owner/founder(s), and named team members (with any phone/email found on the
site). Runs only when the job's `do_research` flag is set — it is slow (an LLM call per lead)
and needs GEMINI_API_KEY / GOOGLE_AI_STUDIO_API_KEY in .env.

Team data comes from the business's OWN website (home / about / team / contact / leadership
pages). LinkedIn is not scraped directly (login-walled, ban risk) — the company LinkedIn URL
captured during enrichment is passed to the model as a hint only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Callable

import httpx
from selectolax.parser import HTMLParser

from webscraper.config import settings
from webscraper.enrich import HEADERS, _fetch
from webscraper.extractors import contact_page_links, extract_emails
from webscraper.store import Store, now_iso

log = logging.getLogger("webscraper.research")

_TEAM_HINT = re.compile(r"about|team|our-people|leadership|founder|management|staff|doctors|meet", re.I)
_MODEL = "gemini-flash-latest"

_PROMPT = """You are analysing a business from the text of its own website.
Business name: {name}
Category: {category}
Website: {website}
Company LinkedIn (if any): {linkedin}

WEBSITE TEXT (truncated):
\"\"\"
{text}
\"\"\"

Return ONLY a JSON object with these keys:
- "summary": one or two sentences on what the business does (plain, factual).
- "owner": the owner / founder / principal name if clearly stated, else null.
- "team": array (max 8) of {{"name": str, "role": str|null, "phone": str|null, "email": str|null}} for named people on the site. Empty array if none.
Do not invent people or numbers. Use null when unknown."""


def _gemini_key() -> str | None:
    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_AI_STUDIO_API_KEY") or os.getenv("GOOGLE_API_KEY")


def _text_of(html: str) -> str:
    tree = HTMLParser(html)
    for tag in tree.css("script,style,noscript,svg"):
        tag.decompose()
    body = tree.body or tree
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", body.text(separator="\n"))).strip()


async def _gather_text(client: httpx.AsyncClient, website: str) -> str:
    url = website if "://" in website else "http://" + website
    home = await _fetch(client, url)
    if home is None and url.startswith("http://"):
        url = "https://" + url[7:]
        home = await _fetch(client, url)
    if not home:
        return ""
    parts = [_text_of(home)]
    # prefer about/team pages
    links = [l for l in contact_page_links(home, url) if _TEAM_HINT.search(l)][:3]
    for l in links:
        p = await _fetch(client, l)
        if p:
            parts.append(_text_of(p))
    return "\n\n".join(parts)[:14000]


async def _ask_gemini(client: httpx.AsyncClient, key: str, prompt: str) -> dict[str, Any] | None:
    try:
        r = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{_MODEL}:generateContent?key={key}",
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1200,
                                       "responseMimeType": "application/json",
                                       "thinkingConfig": {"thinkingBudget": 0}}},
            timeout=40)
        r.raise_for_status()
        txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
        log.warning("gemini research failed: %s", e)
        return None


async def research_places(store: Store, rows: list[dict[str, Any]], concurrency: int = 3,
                          on_progress: Callable[[dict[str, Any], str], None] | None = None,
                          should_stop: Callable[[], bool] | None = None) -> dict[str, int]:
    key = _gemini_key()
    should_stop = should_stop or (lambda: False)
    counts = {"done": 0, "skipped": 0, "failed": 0}
    if not key:
        for r in rows:
            store.update_enrichment(r["job_id"], r["place_key"], {"research_status": "no_key"})
        counts["skipped"] = len(rows)
        return counts
    sem = asyncio.Semaphore(concurrency)
    timeout = httpx.Timeout(45.0, connect=10.0)
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=timeout, verify=False) as client:
        async def one(r: dict[str, Any]) -> None:
            if should_stop():
                return
            website = r.get("website")
            if not website:
                store.update_enrichment(r["job_id"], r["place_key"], {"research_status": "no_website"})
                counts["skipped"] += 1
                if on_progress: on_progress(r, "skipped")
                return
            async with sem:
                text = await _gather_text(client, website)
                if not text:
                    store.update_enrichment(r["job_id"], r["place_key"], {"research_status": "failed"})
                    counts["failed"] += 1
                    if on_progress: on_progress(r, "failed")
                    return
                site_emails = extract_emails(text)
                data = await _ask_gemini(client, key, _PROMPT.format(
                    name=r.get("name") or "", category=r.get("category") or "", website=website,
                    linkedin=r.get("linkedin") or "none", text=text))
            if not data:
                store.update_enrichment(r["job_id"], r["place_key"], {"research_status": "failed"})
                counts["failed"] += 1
                if on_progress: on_progress(r, "failed")
                return
            team = data.get("team") or []
            if isinstance(team, list):
                team = [t for t in team if isinstance(t, dict) and t.get("name")][:8]
            else:
                team = []
            store.update_enrichment(r["job_id"], r["place_key"], {
                "summary": (data.get("summary") or None),
                "owner": (data.get("owner") or None),
                "team": json.dumps(team, ensure_ascii=False),
                "research_status": "done",
                "researched_at": now_iso(),
            })
            counts["done"] += 1
            if on_progress: on_progress(r, "done")
        await asyncio.gather(*(one(r) for r in rows))
    return counts
