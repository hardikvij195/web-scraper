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


def _gemini_error_text(e: BaseException) -> str:
    """One line a user can act on. Job #17 (2026-08-26, Mac) ran 50 leads through a Gemini
    key that was out of quota: every call 429'd, the lane still said "50 done" and the CRM
    showed no summary/owner with no reason anywhere. The status code is the whole story."""
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code == 429:
            return "Gemini quota exhausted (HTTP 429) — replace GEMINI_API_KEY (Lead Finder Setup or the agent's .env)"
        if code in (401, 403):
            return f"Gemini key rejected (HTTP {code}) — check GEMINI_API_KEY"
        return f"Gemini HTTP {code}"
    return f"Gemini call failed: {type(e).__name__}: {str(e)[:120]}"


def _openai_compat_error_text(name: str, model: str, code: int | None,
                              body_snippet: str | None = None) -> str:
    """Same idea as `_gemini_error_text`, for the 6 OpenAI-compatible fallback providers
    (user report 2026-09-04: "groq: HTTP 404" logged with no explanation of what it means
    or how to fix it). These providers share the OpenAI chat-completions error shape, so
    one status-code map covers all of them; the model id is included because 404 here is
    almost always "this exact model id doesn't exist on this provider" rather than a
    reachability problem — unlike a website's HTTP 404, which is the site itself."""
    if code == 429:
        return f"{name}: rate/quota limit hit (HTTP 429) — its free tier resets on its own schedule (often daily); either wait or pick a different provider in Lead Finder → AI APIs"
    if code == 404:
        return (f"{name}: model '{model}' not found (HTTP 404) — the provider does not "
                f"recognise this model id, usually because it was renamed/retired on their "
                f"end. Fix: Lead Finder → AI APIs → {name.title()}, pick a current model "
                f"from the list (or check {name}'s docs for the live id)")
    if code in (401, 403):
        return f"{name}: key rejected (HTTP {code}) — check the {name.upper()}_API_KEY in Lead Finder → Setup & Tokens"
    if code == 400:
        return f"{name}: request rejected (HTTP 400){f' — {body_snippet}' if body_snippet else ''} — usually a bad/unsupported model id or parameter for '{model}'"
    if code is not None and code >= 500:
        return f"{name}: server error (HTTP {code}) — {name}'s own service is having trouble, not a config issue; it will likely work again on retry"
    if code is not None:
        return f"{name}: HTTP {code}"
    return f"{name}: could not reach the API (network/timeout, not a key or model issue)"


#: OpenAI-compatible fallbacks, tried in order after Gemini — first one with a key wins,
#: any failure moves on (T208, after the Gemini quota outage of 2026-08-26). Model per
#: provider overridable with AI_RESEARCH_MODEL_<NAME>; order with AI_RESEARCH_PROVIDERS.
_PROVIDERS: list[tuple[str, str, str, str]] = [
    # name, env key, base url, default model
    ("groq",       "GROQ_API_KEY",       "https://api.groq.com/openai/v1",       "openai/gpt-oss-20b"),
    ("cerebras",   "CEREBRAS_API_KEY",   "https://api.cerebras.ai/v1",           "gpt-oss-120b"),
    # ':free' suffix — OpenRouter's paid catalogue is the same model IDs without it;
    # dropping the suffix silently starts billing (user directive 2026-09-03: default
    # to free tiers everywhere they exist).
    # 2026-09-05: OpenRouter dropped `openai/gpt-oss-20b:free` (404) and NVIDIA retired
    # `meta/llama-3.3-70b-instruct` (410) — every research call on jobs #45/#46 fell
    # through both. Picked from each catalogue's live /models on that day.
    ("openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1",         "nvidia/nemotron-3-super-120b-a12b:free"),
    ("nvidia",     "NVIDIA_API_KEY",     "https://integrate.api.nvidia.com/v1",  "openai/gpt-oss-20b"),
    ("xai",        "XAI_API_KEY",        "https://api.x.ai/v1",                  "grok-3-mini"),
    ("openai",     "OPENAI_API_KEY",     "https://api.openai.com/v1",            "gpt-4o-mini"),
]


def _provider_order() -> list[str]:
    raw = os.getenv("AI_RESEARCH_PROVIDERS") or "gemini," + ",".join(p[0] for p in _PROVIDERS)
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def available_providers() -> list[str]:
    """Names with a key set, in the order they will be tried."""
    out = []
    for name in _provider_order():
        if name == "gemini" and _gemini_key():
            out.append("gemini")
        for p in _PROVIDERS:
            if p[0] == name and os.getenv(p[1]):
                out.append(name)
    return out


#: One LLM call's outcome, whether it succeeded or not — CRM-side token/cost tracking
#: (2026-09-04) needs every ATTEMPT, not just the winning one, so a run that fell
#: through 3 rate-limited free providers before Gemini finally answered shows all 4.
def _usage_row(provider: str, model: str | None, ok: bool, status_code: int | None = None,
               error: str | None = None, prompt_tokens: int | None = None,
               completion_tokens: int | None = None) -> dict[str, Any]:
    return {"provider": provider, "model": model, "ok": ok, "status_code": status_code,
            "error": error, "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}


async def _ask_openai_compat(client: httpx.AsyncClient, name: str, key: str, base: str, model: str,
                             prompt: str, errors: dict[str, Any] | None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    try:
        r = await client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "temperature": 0.2, "max_tokens": 1200,
                  "messages": [{"role": "system", "content": "Reply with a single JSON object only."},
                               {"role": "user", "content": prompt}]},
            timeout=45)
        r.raise_for_status()
        body = r.json()
        usage = body.get("usage") or {}   # OpenAI-standard field name — every one of these 6 providers uses it
        txt = body["choices"][0]["message"]["content"] or ""
        data = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
        return data, _usage_row(name, model, True, r.status_code,
                                 prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"))
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as e:
        log.warning("%s research failed: %s", name, e)
        code = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
        # Body snippet only for 400s — that's the one code where the provider's own
        # message (bad param name, unsupported field) is worth surfacing verbatim;
        # for the others the status code alone already says what to do (see below).
        snippet = None
        if code == 400 and isinstance(e, httpx.HTTPStatusError):
            try:
                snippet = (e.response.json().get("error", {}) or {}).get("message", "")[:160] or None
            except Exception:                                     # noqa: BLE001
                snippet = e.response.text[:160] or None
        msg = _openai_compat_error_text(name, model, code, snippet)
        if errors is not None:
            errors["error"] = msg
        return None, _usage_row(name, model, False, code, msg)


async def _ask_llm(client: httpx.AsyncClient, prompt: str, errors: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Walk the provider chain; the first answer wins. `errors['error']` keeps the LAST
    failure and `errors['provider']` the provider that answered. The second return value
    is every attempt made (2026-09-04, CRM token/cost tracking) — a run that fell through
    3 rate-limited free providers before one answered ships all 4 attempts, not just the
    winner, so a 429 shows up even on a lead that ultimately succeeded."""
    tried = 0
    attempts: list[dict[str, Any]] = []
    for name in _provider_order():
        if name == "gemini":
            key = _gemini_key()
            if not key:
                continue
            tried += 1
            data, usage = await _ask_gemini(client, key, prompt, errors)
        else:
            spec = next((p for p in _PROVIDERS if p[0] == name), None)
            if not spec or not os.getenv(spec[1]):
                continue
            tried += 1
            model = os.getenv(f"AI_RESEARCH_MODEL_{name.upper()}") or spec[3]
            data, usage = await _ask_openai_compat(client, name, os.environ[spec[1]], spec[2], model, prompt, errors)
        attempts.append(usage)
        if data:
            errors["provider"] = name
            return data, attempts
    if tried == 0:
        errors["error"] = "no AI key configured"
    else:
        errors["gemini_failed"] = errors.get("gemini_failed", 0) + 1   # counts "all providers failed"
    return None, attempts


async def _ask_gemini(client: httpx.AsyncClient, key: str, prompt: str,
                      errors: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    # The other 6 providers already honour AI_RESEARCH_MODEL_<NAME> (set by the
    # CRM's AI APIs tab); Gemini was hardcoded to _MODEL and could not be changed
    # from there. Same override, same fallback.
    model = os.getenv("AI_RESEARCH_MODEL_GEMINI") or _MODEL
    try:
        r = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1200,
                                       "responseMimeType": "application/json",
                                       "thinkingConfig": {"thinkingBudget": 0}}},
            timeout=40)
        r.raise_for_status()
        body = r.json()
        usage = body.get("usageMetadata") or {}
        txt = body["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
        return data, _usage_row("gemini", model, True, r.status_code,
                                 prompt_tokens=usage.get("promptTokenCount"),
                                 completion_tokens=usage.get("candidatesTokenCount"))
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
        log.warning("gemini research failed: %s", e)
        msg = _gemini_error_text(e)
        code = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
        if errors is not None:
            errors["error"] = msg
        return None, _usage_row("gemini", model, False, code, msg)


async def research_places(store: Store, rows: list[dict[str, Any]], concurrency: int = 3,
                          on_progress: Callable[[dict[str, Any], str], None] | None = None,
                          should_stop: Callable[[], bool] | None = None) -> dict[str, int]:
    key = _gemini_key()
    should_stop = should_stop or (lambda: False)
    counts: dict[str, Any] = {"done": 0, "skipped": 0, "failed": 0}
    if not available_providers():
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
                data, attempts = await _ask_llm(client, _PROMPT.format(
                    name=r.get("name") or "", category=r.get("category") or "", website=website,
                    linkedin=r.get("linkedin") or "none", text=text), counts)
            for a in attempts:
                store.record_ai_usage(job_id=r["job_id"], place_key=r["place_key"], kind="research", **a)
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
