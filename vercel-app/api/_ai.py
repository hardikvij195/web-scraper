"""AI endpoints running on the MEMBER's own keys (Gemini first, then OpenAI, then server fallback)."""
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
        if out:
            return out
    if k.get("openai_key"):
        out = _openai(k["openai_key"], prompt)
        if out:
            return out
    server_key = os.getenv("GEMINI_API_KEY")
    if server_key:
        return _gemini(server_key, prompt)
    return None


@router.get("/suggest")
def suggest(q: str, user: User = Depends(current_user)):
    q = (q or "").strip()
    if len(q) < 2:
        return {"source": "none", "keywords": []}
    prompt = (f"You help build Google Maps lead-generation searches. For the business type {q!r}, "
              "list 10 other Google Maps search keywords that find the same or closely related businesses. "
              "Reply as a JSON array of short strings only, no prose.")
    text = _llm(user.id, prompt)
    if text:
        try:
            kws = [str(x).strip() for x in _json.loads(text[text.find("["): text.rfind("]") + 1])
                   if str(x).strip()][:12]
            if kws:
                return {"source": "ai", "keywords": kws}
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
                    if s and s.lower() != q.lower() and s not in kws:
                        kws.append(s)
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
    if not rows:
        raise HTTPException(404, "no such lead")
    lead = rows[0]
    facts = {k: lead.get(k) for k in ("name", "category", "address", "phone", "email", "website",
                                      "instagram", "facebook", "rating", "reviews_count")}
    text = _llm(lead["user_id"], "Summarize this business as a sales lead in 2-3 sentences, "
                                 "mention an outreach angle. Facts: " + _json.dumps(facts, ensure_ascii=False))
    if not text:
        raise HTTPException(400, "no working AI key — add one in Settings")
    sb_patch("web_scraper_leads", {"ai_summary": text.strip()},
             {"user_id": f"eq.{lead['user_id']}", "place_key": f"eq.{body.place_key}"})
    return {"summary": text.strip()}
