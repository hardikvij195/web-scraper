"""W215 (CRM T1557): WhatsApp per-NUMBER check budget + cooldown.

Owner rule 2026-11-09: "do 1k checks on poco biz, iphone, realme papa, realme biz and mi and take a
cooldown for 2 hrs and then resume". The night of 11-08/09 all five numbers were logged out by
WhatsApp after 1.3k-4k checks each. The SAME number is linked on several machines (MAC + DELL + PC),
so the budget is counted per NUMBER (the 10-digit suffix of the account name, e.g.
`poco_biz_9315646352`) in the CRM (`wa_number_take` RPC through the `lead-finder-agent` Edge
Function, action `wa_budget`), reserved in small blocks (`RESERVE`) to keep RPC traffic low.

When the CRM refuses, that account leaves the rotation until `cooldown_until` (the lane drops it
from its session list, its Chrome closes, the other numbers carry on) and comes back by itself.
CRM unreachable -> a local per-number counter enforces the same cap (fail-safe).

Settings (lead_gen_settings -> env, live-refreshed): WA_NUMBER_CAP (default 1000; 0 = off),
WA_NUMBER_COOLDOWN_MIN (default 120), WA_NUMBER_CAP_NUMBERS (comma list of 10-digit numbers, or
`all`; default `all`).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

#: Owner 2026-11-09 (after OnePlus 9773603818 was logged out too): every number is capped by default.
DEFAULT_NUMBERS = "all"
RESERVE = 10                     #: checks reserved per CRM call
_NUM_RE = re.compile(r"(\d{10})$")


def account_number(name: str | None) -> str | None:
    """`poco_biz_9315646352` -> `9315646352`; no 10-digit suffix -> None (never capped)."""
    if not name:
        return None
    m = _NUM_RE.search(str(name).strip())
    return m.group(1) if m else None


def _int_env(key: str, default: int) -> int:
    try:
        return int(float(str(os.getenv(key, "")).strip()))
    except (TypeError, ValueError):
        return default


def number_cap() -> int:
    return _int_env("WA_NUMBER_CAP", 1000)


def cooldown_sec() -> float:
    return max(1, _int_env("WA_NUMBER_COOLDOWN_MIN", 120)) * 60.0


def applies(number: str | None) -> bool:
    if not number or number_cap() <= 0:
        return False
    raw = (os.getenv("WA_NUMBER_CAP_NUMBERS") or DEFAULT_NUMBERS).strip().lower()
    if raw in ("all", "*"):
        return True
    return number in {d[-10:] for d in ("".join(ch for ch in p if ch.isdigit()) for p in raw.split(",")) if len(d) >= 10}


def _parse_ts(v: Any) -> float | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Budget:
    """Thread-safe per-number budget. `remote(number, n)` returns the RPC answer
    {allowed:int, cooldown_until:iso|None} or raises / returns None when the CRM is unreachable."""

    def __init__(self, remote: Callable[[str, int], dict | None] | None = None,
                 now: Callable[[], float] = time.time, state_path: Path | None = None):
        self.remote = remote
        self.now = now
        self.state_path = state_path
        self._lock = threading.Lock()
        self._left: dict[str, int] = {}          # number -> reserved checks not yet used
        self._cool: dict[str, float] = {}        # number -> cooldown end (epoch)
        self._local: dict[str, int] = {}         # number -> checks in the current local window
        self._announced: set[str] = set()        # numbers whose cooldown start was reported
        self._load()

    # -- local fail-safe state ---------------------------------------------------------------
    def _load(self) -> None:
        if not self.state_path:
            return
        try:
            d = json.loads(self.state_path.read_text("utf-8"))
            self._local = {str(k): int(v) for k, v in (d.get("local") or {}).items()}
            self._cool = {str(k): float(v) for k, v in (d.get("cool") or {}).items() if float(v) > self.now()}
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _save(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({"local": self._local, "cool": self._cool}), "utf-8")
        except OSError:
            pass

    # -- queries -----------------------------------------------------------------------------
    def cooling_until(self, name: str) -> float | None:
        """Cooldown end for `name`'s number, or None when it may check."""
        num = account_number(name)
        if not applies(num):
            return None
        with self._lock:
            until = self._cool.get(num)
            return until if until and until > self.now() else None

    def cooling_names(self, names) -> set[str]:
        return {n for n in names if self.cooling_until(n)}

    # -- the gate ----------------------------------------------------------------------------
    def take(self, name: str) -> tuple[bool, float | None, str | None]:
        """One check on `name`. Returns (allowed, cooldown_until, event); event is "start" the
        first time a number's cooldown is seen, "end" when a finished cooldown is lifted."""
        num = account_number(name)
        if not applies(num):
            return True, None, None
        with self._lock:
            event = None
            t = self.now()
            until = self._cool.get(num)
            if until:
                if until > t:
                    ev = None if num in self._announced else "start"
                    self._announced.add(num)
                    return False, until, ev
                self._cool.pop(num, None)                       # cooldown over -> resume
                self._announced.discard(num)
                self._local[num] = 0
                self._save()
                event = "end"
            if self._left.get(num, 0) > 0:
                self._left[num] -= 1
                self._local[num] = self._local.get(num, 0) + 1
                return True, None, event
            ans = None
            if self.remote is not None:
                try:
                    ans = self.remote(num, RESERVE)
                except Exception as e:                            # noqa: BLE001
                    log.warning("WhatsApp number budget: CRM unreachable (%s) — local counter", e)
                    ans = None
            if isinstance(ans, dict) and "allowed" in ans:
                allowed = max(0, int(ans.get("allowed") or 0))
                cu = _parse_ts(ans.get("cooldown_until"))
                if allowed <= 0:
                    self._cool[num] = cu if cu and cu > t else t + cooldown_sec()
                    self._left[num] = 0
                    self._local[num] = 0
                    self._announced.add(num)
                    self._save()
                    return False, self._cool[num], "start"
                self._left[num] = allowed - 1
                self._local[num] = self._local.get(num, 0) + 1
                return True, None, event
            # fail-safe: the CRM did not answer — enforce the cap on this machine's own count
            if self._local.get(num, 0) >= number_cap():
                self._cool[num] = t + cooldown_sec()
                self._local[num] = 0
                self._announced.add(num)
                self._save()
                return False, self._cool[num], "start"
            self._local[num] = self._local.get(num, 0) + 1
            if self._local[num] % 50 == 0:
                self._save()
            return True, None, event


def _default_state() -> Path:
    try:
        from webscraper.config import settings
        return Path(settings.wa_profiles_dir).parent / "wa_number_budget.json"
    except Exception:                                             # noqa: BLE001
        return Path("data") / "wa_number_budget.json"


BUDGET = Budget(state_path=_default_state())


def set_remote(fn: Callable[[str, int], dict | None] | None) -> None:
    """The agent wires the CRM call in at start (`CrmCloud.wa_budget`); CLI runs stay local."""
    BUDGET.remote = fn


def fmt_until(until: float | None) -> str:
    return datetime.fromtimestamp(until).strftime("%H:%M") if until else "?"
