"""W169 (CRM T1047): the Chrome RAM diet — one lean flag list for every Chrome this repo
launches (Maps collector + opener, WhatsApp verify / login / probe, the enrichment browser).

Owner question 2026-10-07: "is there a way we can optimize the chrome windows for minimum
usage of RAM in all systems in lead finder?" — this module is the shared answer. Defaults ON;
`CHROME_LEAN_ARGS=0` turns the whole list off, `CHROME_JS_HEAP_MB[__<KIND>]` tunes the V8 heap
cap per kind (0 = no cap). Never `--single-process` / `--no-sandbox` (FORBIDDEN below).

What each flag buys:
  --disable-features=site-per-process,IsolateOrigins  one renderer per tab instead of one per
                                                        origin (Maps embeds ~6 origins per panel)
  --disable-features=...,TranslateUI,BackForwardCache  no translate ranker, no cached back/forward
                                                        documents kept alive beside the live one
  --renderer-process-limit=2                            hard cap on renderer processes
  --disable-dev-shm-usage                               /dev/shm -> /tmp on Linux (no-op elsewhere)
  --no-first-run / --no-default-browser-check           no first-run tabs / prompts (= RESTORE_BUBBLE_ARGS)
  --disable-sync / --disable-component-update           no sync service, no component downloader
  --disable-background-networking                       no safe-browsing / variations / update fetches
  --metrics-recording-only                              UMA in memory only, never uploaded
  --disable-extensions                                  no extension host process
  --js-flags=--max-old-space-size=N                     V8 old-space cap per renderer (MB)
"""
from __future__ import annotations

import os

#: Flags this module must never emit — `--single-process` trades RAM for a crash of the whole
#: browser on one bad tab, `--no-sandbox` is a security downgrade. A test pins both out.
FORBIDDEN = ("--single-process", "--no-sandbox")

DISABLE_FEATURES = ("site-per-process", "IsolateOrigins", "TranslateUI", "BackForwardCache")

#: V8 old-space cap (MB) per Chrome kind. WhatsApp Web syncing a heavy-history account holds
#: more live JS than a Maps panel (W143) — 256 MB there would OOM the renderer mid-sync.
JS_HEAP_MB = {"maps": 256, "enrich": 256, "wa": 512}

#: `browser_fetch.IGNORE_DEFAULT_ARGS` strips these two from Playwright's OWN defaults because a
#: real Chrome never carries them (a Cloudflare tell) — the enrichment Chrome must not get them
#: back through the diet.
STEALTH_SENSITIVE = ("--disable-component-update", "--disable-extensions")

_OFF = ("0", "false", "no", "off")


def device_upper() -> str:
    """The agent's device name upper-cased (`2 - MAC`); '' outside the agent."""
    try:
        from webscraper.agent import DEVICE_NAME
    except Exception:                                             # noqa: BLE001
        return ""
    return (DEVICE_NAME or "").upper()


def flag_enabled(name: str, default: str = "1") -> bool:
    """W181: an on/off env flag with a per-device form — `<NAME>__<DEVICE UPPER>` (a CRM
    `lead_gen_settings` key, applied to every machine) > `<NAME>` > `default`. MAC's Maps
    renderer crashes under the W169 diet while the Windows laptops are fine, so the owner
    can turn `CHROME_LEAN_ARGS` / `MAPS_BLOCK_ASSETS` off for ONE machine."""
    dev = device_upper()
    raw = os.getenv(f"{name}__{dev}") if dev else None
    if raw is None or not str(raw).strip():
        raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        raw = default
    return str(raw).strip().lower() not in _OFF


def enabled() -> bool:
    return flag_enabled("CHROME_LEAN_ARGS")


def js_heap_mb(kind: str) -> int:
    """`CHROME_JS_HEAP_MB__<KIND>` > `CHROME_JS_HEAP_MB` > the per-kind default; 0 = no cap."""
    raw = os.getenv(f"CHROME_JS_HEAP_MB__{kind.upper()}") or os.getenv("CHROME_JS_HEAP_MB")
    if raw is None or not str(raw).strip():
        return JS_HEAP_MB.get(kind, 256)
    try:
        return max(0, int(str(raw).strip()))
    except ValueError:
        return JS_HEAP_MB.get(kind, 256)


def lean_args(kind: str) -> list[str]:
    """The diet for one Chrome kind: "maps" | "wa" | "enrich". [] when `CHROME_LEAN_ARGS=0`."""
    if not enabled():
        return []
    out = [
        "--disable-features=" + ",".join(DISABLE_FEATURES),
        "--renderer-process-limit=2",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-component-update",
        "--disable-extensions",
        "--metrics-recording-only",
        "--disable-background-networking",
    ]
    heap = js_heap_mb(kind)
    if heap:
        out.append(f"--js-flags=--max-old-space-size={heap}")
    if kind == "enrich":
        out = [a for a in out if a not in STEALTH_SENSITIVE]
    assert not any(a in FORBIDDEN for a in out)
    return out


def _split(flag: str) -> tuple[str, str | None]:
    name, sep, value = flag.partition("=")
    return name, (value if sep else None)


def merge_args(*groups) -> list[str]:
    """Merge flag lists without duplicates. Keyed on the flag NAME: the first value wins
    (an explicit per-launch flag beats the diet), except `--disable-features`, whose values
    are unioned, and `--js-flags`, whose values are joined (Chrome takes one such switch)."""
    order: list[str] = []
    values: dict[str, str | None] = {}
    for group in groups:
        for flag in group or ():
            name, value = _split(flag)
            if name not in values:
                order.append(name)
                values[name] = value
                continue
            if name == "--disable-features" and value:
                have = [v for v in (values[name] or "").split(",") if v]
                values[name] = ",".join(have + [v for v in value.split(",") if v and v not in have])
            elif name == "--js-flags" and value and value not in (values[name] or ""):
                values[name] = f"{values[name]} {value}".strip() if values[name] else value
    return [n if values[n] is None else f"{n}={values[n]}" for n in order]
