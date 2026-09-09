"""W79 (CRM T478): several keys per AI provider. The chain tries every key of a provider
(position order) before it moves on to the next provider; a 429'd key cools down for
KEY_COOLDOWN_SEC inside this process and a 401/403'd key is not retried at all. Keys come
from the env the CRM's config push writes: GROQ_API_KEY, GROQ_API_KEY_2, GROQ_API_KEY_3 …
(the list stops at the first gap). No network: httpx responses are built by hand."""
import asyncio
import json

import httpx
import pytest

from webscraper import research


_ENV_PREFIXES = ("GEMINI_API_KEY", "GOOGLE_AI_STUDIO_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY",
                 "CEREBRAS_API_KEY", "OPENROUTER_API_KEY", "NVIDIA_API_KEY", "OPENAI_API_KEY",
                 "XAI_API_KEY", "AI_RESEARCH_")


@pytest.fixture
def clean_env(monkeypatch):
    """No key from the developer's own .env may leak into these tests."""
    import os
    for name in list(os.environ):
        if name.startswith(_ENV_PREFIXES):
            monkeypatch.delenv(name, raising=False)
    research.reset_key_state()
    yield monkeypatch
    research.reset_key_state()


class FakeClient:
    """Stands in for httpx.AsyncClient: `handler(key) -> status` decides each call."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[str] = []      # the key each call presented, in order

    async def post(self, url, headers=None, json=None, timeout=None):
        key = (headers or {}).get("Authorization", "").replace("Bearer ", "") or url.split("key=")[-1]
        self.calls.append(key)
        status = self.handler(key)
        req = httpx.Request("POST", url)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "nope"}}, request=req)
        if "generativelanguage" in url:
            body = {"candidates": [{"content": {"parts": [{"text": json_dumps({"summary": "ok by " + key})}]}}],
                    "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2}}
        else:
            body = {"choices": [{"message": {"content": json_dumps({"summary": "ok by " + key})}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
        return httpx.Response(200, json=body, request=req)


def json_dumps(o):
    return json.dumps(o)


def _ask(client):
    errors: dict = {}
    data, attempts = asyncio.run(research._ask_llm(client, "prompt", errors))
    return data, attempts, errors


def test_keys_for_stops_at_the_first_gap(clean_env):
    clean_env.setenv("GROQ_API_KEY", "k1")
    clean_env.setenv("GROQ_API_KEY_3", "k3")           # no _2 → _3 is never reached
    assert research._keys_for("groq") == ["k1"]
    clean_env.setenv("GROQ_API_KEY_2", "k2")
    assert research._keys_for("groq") == ["k1", "k2", "k3"]
    # gemini keeps its legacy aliases for key 1 only
    clean_env.setenv("GOOGLE_AI_STUDIO_API_KEY", "g1")
    clean_env.setenv("GEMINI_API_KEY_2", "g2")
    assert research._keys_for("gemini") == ["g1", "g2"]


def test_second_key_answers_when_the_first_is_rate_limited(clean_env):
    clean_env.setenv("AI_RESEARCH_PROVIDERS", "groq")
    clean_env.setenv("GROQ_API_KEY", "k1")
    clean_env.setenv("GROQ_API_KEY_2", "k2")
    client = FakeClient(lambda key: 429 if key == "k1" else 200)

    data, attempts, errors = _ask(client)

    assert data == {"summary": "ok by k2"}
    assert errors["provider"] == "groq"
    assert client.calls == ["k1", "k2"]
    assert [(a["key_index"], a["ok"], a["status_code"]) for a in attempts] == [(1, False, 429), (2, True, 200)]


def test_rejected_key_moves_to_the_next_key_and_stays_out(clean_env):
    clean_env.setenv("AI_RESEARCH_PROVIDERS", "groq")
    clean_env.setenv("GROQ_API_KEY", "k1")
    clean_env.setenv("GROQ_API_KEY_2", "k2")
    client = FakeClient(lambda key: 401 if key == "k1" else 200)

    data, attempts, _ = _ask(client)
    assert data and client.calls == ["k1", "k2"]
    assert attempts[0]["key_index"] == 1 and attempts[0]["status_code"] == 401

    # a 401 is not a quota — the key is wrong; this process never presents it again
    data, attempts, _ = _ask(client)
    assert data and client.calls == ["k1", "k2", "k2"]
    assert [a["key_index"] for a in attempts] == [2]


def test_every_key_of_a_provider_is_tried_before_the_next_provider(clean_env):
    clean_env.setenv("AI_RESEARCH_PROVIDERS", "groq,cerebras")
    clean_env.setenv("GROQ_API_KEY", "k1")
    clean_env.setenv("GROQ_API_KEY_2", "k2")
    clean_env.setenv("CEREBRAS_API_KEY", "c1")
    client = FakeClient(lambda key: 500 if key.startswith("k") else 200)

    data, attempts, errors = _ask(client)

    assert data == {"summary": "ok by c1"}
    assert errors["provider"] == "cerebras"
    assert client.calls == ["k1", "k2", "c1"]
    assert [(a["provider"], a["key_index"], a["ok"]) for a in attempts] == [
        ("groq", 1, False), ("groq", 2, False), ("cerebras", 1, True)]


def test_a_rate_limited_key_is_skipped_within_the_cooldown(clean_env):
    clean_env.setenv("AI_RESEARCH_PROVIDERS", "groq")
    clean_env.setenv("GROQ_API_KEY", "k1")
    clean_env.setenv("GROQ_API_KEY_2", "k2")
    client = FakeClient(lambda key: 429 if key == "k1" else 200)

    _ask(client)
    assert client.calls == ["k1", "k2"]
    data, attempts, _ = _ask(client)                 # k1 is cooling down: straight to k2
    assert data and client.calls == ["k1", "k2", "k2"]
    assert [a["key_index"] for a in attempts] == [2]

    # cooldown over → key 1 is tried again first (sticky order, not round-robin)
    clean_env.setattr(research, "KEY_COOLDOWN_SEC", 0)
    research.reset_key_state()
    _ask(client)
    assert client.calls[-2:] == ["k1", "k2"]


def test_all_keys_failing_counts_as_one_provider_failure(clean_env):
    clean_env.setenv("AI_RESEARCH_PROVIDERS", "groq")
    clean_env.setenv("GROQ_API_KEY", "k1")
    clean_env.setenv("GROQ_API_KEY_2", "k2")
    client = FakeClient(lambda key: 429)

    data, attempts, errors = _ask(client)

    assert data is None
    assert len(attempts) == 2 and errors["gemini_failed"] == 1
    assert "groq" in errors["error"]


def test_available_providers_needs_only_key_one(clean_env):
    clean_env.setenv("AI_RESEARCH_PROVIDERS", "gemini,groq,openai")
    clean_env.setenv("GROQ_API_KEY_2", "orphan")      # no GROQ_API_KEY → groq has no keys
    clean_env.setenv("OPENAI_API_KEY", "o1")
    assert research.available_providers() == ["openai"]
