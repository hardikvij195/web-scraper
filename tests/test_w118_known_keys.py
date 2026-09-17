"""W118 (CRM T765) — cross-machine "already scraped" keys.

1. `CrmCloud.known_keys`: happy path, an old/unknown-action Edge Fn (400+), and a transient
   network failure all round-trip to a plain `set[str]` — never raise into the caller.
2. `server.fetch_known_cloud_keys`: merges local + cloud, degrades to empty on a fetch
   failure (cloud down -> local-only, since the caller ORs this into its own local set), and
   never calls the fetcher at all for a job without `unique_new`.
No network: `CrmCloud._post` and the fetcher are stubbed.
"""
from __future__ import annotations

import httpx
import pytest

from webscraper import agent, server


class _Resp:
    def __init__(self, status_code: int, body: object):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _cloud() -> agent.CrmCloud:
    return agent.CrmCloud("https://example.invalid", "tok")


def test_known_keys_happy_path(monkeypatch):
    c = _cloud()
    seen = {}

    def fake_post(payload):
        seen.update(payload)
        return _Resp(200, {"keys": ["a1", "a2", "a1"]})

    monkeypatch.setattr(c, "_post", fake_post)
    assert c.known_keys("GB") == {"a1", "a2"}
    assert seen["action"] == "known_keys" and seen["country"] == "GB"


def test_known_keys_old_edge_function_returns_empty(monkeypatch):
    c = _cloud()
    monkeypatch.setattr(c, "_post", lambda payload: _Resp(400, {"error": "unknown action"}))
    assert c.known_keys(None) == set()


def test_known_keys_network_failure_returns_empty(monkeypatch):
    c = _cloud()

    def raises(payload):
        raise httpx.ConnectError("getaddrinfo failed")

    monkeypatch.setattr(c, "_post", raises)
    assert c.known_keys("AU") == set()


def test_known_keys_non_dict_body_returns_empty(monkeypatch):
    c = _cloud()
    monkeypatch.setattr(c, "_post", lambda payload: _Resp(200, None))
    assert c.known_keys("AU") == set()


def test_fetch_known_cloud_keys_merge():
    calls = {"n": 0}

    def fetcher(country):
        calls["n"] += 1
        assert country == "GB"
        return {"x1", "x2"}

    got = server.fetch_known_cloud_keys(True, fetcher, "GB", job_id=1)
    assert got == {"x1", "x2"}
    assert calls["n"] == 1


def test_fetch_known_cloud_keys_failure_is_local_only():
    def raises(country):
        raise RuntimeError("cloud down")

    assert server.fetch_known_cloud_keys(True, raises, "GB", job_id=1) == set()


def test_fetch_known_cloud_keys_no_fetcher_configured():
    assert server.fetch_known_cloud_keys(True, None, "GB", job_id=1) == set()


def test_fetch_known_cloud_keys_never_called_without_unique_new():
    def boom(country):
        raise AssertionError("must not be called when unique_new is False")

    assert server.fetch_known_cloud_keys(False, boom, "GB", job_id=1) == set()
