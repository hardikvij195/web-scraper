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

import logging
import os

log = logging.getLogger("webscraper.chrome_args")

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


#: W181b: the agent exports its label here right after `DEVICE_NAME` is computed / renamed, so
#: the per-device flags never depend on importing `webscraper.agent` from inside a Chrome launch.
DEVICE_ENV = "HVT_AGENT_DEVICE"
_import_warned = False


def device_name() -> str:
    """The agent's device label (`2 - MAC`); '' outside the agent.

    W181b order: `HVT_AGENT_DEVICE` (env, exported by agent.py) > `data/device_name` under
    `config.ROOT` (what the agent persisted) > the lazy `webscraper.agent.DEVICE_NAME` import.
    2 - MAC kept blocking Maps assets after `MAPS_BLOCK_ASSETS__2 - MAC=0` arrived from the
    cloud: the old import-only path returned '' on ANY exception, silently, and the generic
    default won. The import failure is now logged once at WARNING."""
    global _import_warned
    name = (os.getenv(DEVICE_ENV) or "").strip()
    if name:
        return name
    try:
        from webscraper.config import ROOT
        name = (ROOT / "data" / "device_name").read_text(encoding="utf-8").strip()
    except Exception:                                             # noqa: BLE001
        name = ""
    if name:
        return name
    try:
        from webscraper.agent import DEVICE_NAME
    except Exception as e:                                        # noqa: BLE001
        if not _import_warned:
            _import_warned = True
            log.warning("device name unresolved — per-device Chrome-diet flags fall back to the "
                        "generic ones (W181b): %s: %s", type(e).__name__, e)
        return ""
    return (DEVICE_NAME or "").strip()


def device_upper() -> str:
    """The agent's device name upper-cased (`2 - MAC`); '' outside the agent."""
    return device_name().upper()


def flag_lookup(name: str, default: str = "1") -> dict:
    """W181b: the decision AND its inputs — `{enabled, device, per_device, generic}` — so the
    launch log can show why a diet switch landed where it did."""
    dev = device_upper()
    per = os.getenv(f"{name}__{dev}") if dev else None
    gen = os.getenv(name)
    raw = per if per is not None and str(per).strip() else None
    if raw is None:
        raw = gen if gen is not None and str(gen).strip() else default
    return {"enabled": str(raw).strip().lower() not in _OFF, "device": dev, "per_device": per, "generic": gen}


def flag_inputs(name: str) -> str:
    """`[device='2 - MAC' per-device='0' generic=None]` for a log line."""
    d = flag_lookup(name)
    return f"[device={d['device']!r} per-device={d['per_device']!r} generic={d['generic']!r}]"


def flag_enabled(name: str, default: str = "1") -> bool:
    """W181: an on/off env flag with a per-device form — `<NAME>__<DEVICE UPPER>` (a CRM
    `lead_gen_settings` key, applied to every machine) > `<NAME>` > `default`. MAC's Maps
    renderer crashes under the W169 diet while the Windows laptops are fine, so the owner
    can turn `CHROME_LEAN_ARGS` / `MAPS_BLOCK_ASSETS` off for ONE machine."""
    return flag_lookup(name, default)["enabled"]


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
        log.info("Chrome diet off for this device (W181) %s", flag_inputs("CHROME_LEAN_ARGS"))
        return []
    log.info("Chrome diet on for %s Chrome (W169) %s", kind, flag_inputs("CHROME_LEAN_ARGS"))
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
