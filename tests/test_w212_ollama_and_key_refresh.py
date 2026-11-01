"""W212: local Ollama provider goes right after groq; AI keys are live-refreshed from the CRM."""
from webscraper import research, agent


def test_ollama_inserted_after_groq(monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_PROVIDERS", "groq,openrouter,nvidia")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama")
    assert research._provider_order() == ["groq", "ollama", "openrouter", "nvidia"]


def test_ollama_first_without_groq(monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_PROVIDERS", "openrouter,nvidia")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama")
    assert research._provider_order()[0] == "ollama"


def test_no_ollama_without_key(monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_PROVIDERS", "groq,openrouter")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    assert research._provider_order() == ["groq", "openrouter"]


def test_base_override(monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_BASE_OLLAMA", "http://192.168.1.5:11434/v1/")
    assert research._base_for("ollama", "http://localhost:11434/v1") == "http://192.168.1.5:11434/v1"
    monkeypatch.delenv("AI_RESEARCH_BASE_OLLAMA")
    assert research._base_for("ollama", "http://localhost:11434/v1") == "http://localhost:11434/v1"


def test_new_cloud_ai_key_adopted_on_refresh(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY_2", raising=False)
    monkeypatch.delenv("AI_RESEARCH_PROVIDERS", raising=False)

    class C:
        def config(self):
            return {"groq_api_key_2": "k2", "ai_research_providers": "groq,openrouter"}
    agent._CLOUD_ENV.discard("GROQ_API_KEY_2")
    agent._CLOUD_ENV.discard("AI_RESEARCH_PROVIDERS")
    agent._refresh_cloud_config(C(), force=True)
    import os
    assert os.environ["GROQ_API_KEY_2"] == "k2"
    assert os.environ["AI_RESEARCH_PROVIDERS"] == "groq,openrouter"
    monkeypatch.delenv("GROQ_API_KEY_2"); monkeypatch.delenv("AI_RESEARCH_PROVIDERS")
