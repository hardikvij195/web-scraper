"""Proxy rotation for the enrichment fetch ladder (httpx → curl_cffi → browser).

W13 wired ONE optional `ENRICH_PROXY` URL into the TLS and browser tiers. This module adds
`ENRICH_PROXIES` — a comma/newline-separated list — and a small `ProxyPool` that hands the
tiers a proxy in round-robin order, counts consecutive failures per proxy, benches a proxy
after `max_failures` in a row (dead gateway, 407, blocked exit IP) and re-admits it after
`cooldown_sec`. Nothing here talks to the network; the clock is injected so the quarantine
and re-admit rules are unit-testable to the second.

Precedence: `ENRICH_PROXIES` supersedes `ENRICH_PROXY`. With only `ENRICH_PROXY` set there is
NO pool (`get_pool()` → None) and every tier behaves exactly as it did under W13: httpx direct,
curl_cffi + browser through that one proxy. With neither set the ladder never touches this
module.

Credentials never leave the process in a readable form: `redact()` turns
`http://user:pass@gw.example.com:7777` into `gw.example.com:7777`, and that is the only
shape that reaches logs, `enrich_via` and `enrich_error`.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from urllib.parse import urlsplit

log = logging.getLogger("webscraper.proxies")

#: Consecutive failures before a proxy is benched.
DEFAULT_MAX_FAILURES = 3
#: Seconds a benched proxy sits out before it is offered again.
DEFAULT_COOLDOWN_SEC = 300.0

#: Error reasons (the strings stored in `enrich_error`) that blame the PROXY rather than the
#: site: the gateway refused our credentials (407), or the connection to it failed. A plain
#: `http_403` is ambiguous — the exit IP may be burned, or the site may block everyone — so it
#: only counts against a proxy when the same page then reads fine directly (a known-good
#: control; see enrich._with_proxies under ENRICH_PROXY_FIRST).
PROXY_ERRORS = frozenset({"proxy_407", "proxy_connect"})


def parse_proxy_list(raw: str | None) -> list[str]:
    """Split `ENRICH_PROXIES` on commas / newlines / whitespace, drop blanks and duplicates,
    keep order. Accepts `host:port`, `user:pass@host:port` (scheme defaults to http) and full
    URLs. Unparseable entries are skipped with a warning rather than killing the run."""
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for chunk in raw.replace("\n", ",").replace("\r", ",").split(","):
        for token in chunk.split():
            s = token.strip().strip("'\"")
            if not s:
                continue
            if "://" not in s:
                s = "http://" + s
            if not urlsplit(s).hostname:
                log.warning("ENRICH_PROXIES entry unparseable (%r) — skipped", redact(token))
                continue
            if s in seen:
                continue
            seen.add(s)
            out.append(s)
    return out


def redact(url: str | None) -> str | None:
    """`host:port` only — never the user/pass — for logs and the DB. None stays None."""
    if not url:
        return None
    u = urlsplit(url if "://" in url else "http://" + url)
    if not u.hostname:
        # Nothing parseable; still make sure a `user:pass@` never leaks through.
        return url.rsplit("@", 1)[-1]
    return f"{u.hostname}:{u.port}" if u.port else u.hostname


def proxy_arg(url: str | None) -> dict[str, str] | None:
    """A proxy URL as Playwright's `proxy` dict (server + credentials split out), or None."""
    if not url:
        return None
    u = urlsplit(url)
    if not u.hostname:
        log.warning("proxy URL unparseable (%s) — ignoring", redact(url))
        return None
    server = f"{u.scheme or 'http'}://{u.hostname}" + (f":{u.port}" if u.port else "")
    out: dict[str, str] = {"server": server}
    if u.username:
        out["username"] = u.username
    if u.password:
        out["password"] = u.password
    return out


class ProxyPool:
    """Round-robin over a fixed list with per-proxy failure counting and quarantine.

    `next()` returns the next healthy proxy (re-admitting any whose cooldown has elapsed)
    or None when every proxy is benched. `failure(p)` / `success(p)` feed the counters;
    a success resets the streak. Thread-safety is not needed: each enrichment lane runs
    one asyncio loop and reports from it."""

    def __init__(self, urls: list[str], *, max_failures: int = DEFAULT_MAX_FAILURES,
                 cooldown_sec: float = DEFAULT_COOLDOWN_SEC,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._urls = list(dict.fromkeys(u for u in urls if u))
        self._max = max(1, int(max_failures))
        self._cooldown = float(cooldown_sec)
        self._clock = clock
        self._i = 0
        self._fails: dict[str, int] = {u: 0 for u in self._urls}
        self._benched_until: dict[str, float] = {}

    # -- introspection ---------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._urls)

    def __bool__(self) -> bool:
        return bool(self._urls)

    @property
    def urls(self) -> list[str]:
        return list(self._urls)

    def failures(self, url: str) -> int:
        return self._fails.get(url, 0)

    def is_benched(self, url: str) -> bool:
        until = self._benched_until.get(url)
        if until is None:
            return False
        if self._clock() >= until:
            # Cooldown over: re-admit with a clean streak.
            del self._benched_until[url]
            self._fails[url] = 0
            log.info("proxy %s re-admitted after cooldown", redact(url))
            return False
        return True

    def healthy(self) -> list[str]:
        return [u for u in self._urls if not self.is_benched(u)]

    # -- selection -------------------------------------------------------------------
    def next(self) -> str | None:
        """Next healthy proxy in round-robin order, or None if all are benched."""
        n = len(self._urls)
        for _ in range(n):
            u = self._urls[self._i % n]
            self._i = (self._i + 1) % n
            if not self.is_benched(u):
                return u
        return None

    # -- feedback --------------------------------------------------------------------
    def failure(self, url: str, reason: str | None = None) -> bool:
        """Count one failure. Returns True if this one benched the proxy."""
        if url not in self._fails:
            return False
        self._fails[url] += 1
        if self._fails[url] >= self._max and url not in self._benched_until:
            self._benched_until[url] = self._clock() + self._cooldown
            log.warning("proxy %s benched for %.0fs after %d consecutive failures (%s)",
                        redact(url), self._cooldown, self._fails[url], reason or "?")
            return True
        return False

    def success(self, url: str) -> None:
        if url in self._fails:
            self._fails[url] = 0
            self._benched_until.pop(url, None)


# ── process-wide pool built from settings ────────────────────────────────────────────
_pool: ProxyPool | None = None
_pool_built = False


def get_pool() -> ProxyPool | None:
    """The pool configured by `ENRICH_PROXIES`, or None when it is unset (a lone
    `ENRICH_PROXY` keeps the W13 single-proxy path, no pool). Built once per process;
    `reset_pool()` for tests."""
    global _pool, _pool_built
    if not _pool_built:
        from webscraper.config import settings
        urls = list(settings.enrich_proxies)
        _pool = ProxyPool(urls, max_failures=settings.enrich_proxy_max_failures,
                          cooldown_sec=settings.enrich_proxy_cooldown_sec) if urls else None
        _pool_built = True
        if _pool:
            log.info("enrichment proxy pool: %d proxies (%s)", len(_pool),
                     ", ".join(redact(u) or "?" for u in _pool.urls))
    return _pool


def reset_pool() -> None:
    global _pool, _pool_built
    _pool, _pool_built = None, False
