"""Relaunch a Playwright browser that died mid-run, and retry the one unit that was in
flight — instead of losing the whole lane.

Chrome dies on its own: OOM, a crashed renderer, a Windows update, or a human closing the
window on a headed run. Job #3 died exactly that way, mid-feed, with `TargetClosedError`,
and threw away every place it had already collected.

`maps.py` grew this logic first (commit `a544ae7`). It lives here now because the lanes
change (2026-08-23) gave WhatsApp verification its own long-lived browser, which had **no**
recovery at all — a dead WhatsApp Chrome took the whole lane down. Same failure, same fix,
one implementation.

Usage:

    rl = Relauncher(open_fn, on_restart=lambda where, n: emit(...))
    ctx, page = rl.open()
    ...
    except PWError as e:
        if not is_closed(e) or not rl.recover(f"place {i}"):
            raise
        ctx, page = rl.current      # retry this one unit on the fresh browser
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

log = logging.getLogger("webscraper.browser_recovery")

#: A browser that cannot start at all must fail fast rather than loop.
MAX_RELAUNCH = 3
#: Seconds to wait between closing the dead context and launching a fresh one.
RELAUNCH_SETTLE_SEC = 2.0


def is_closed(e: Exception) -> bool:
    """True when the exception means "the browser/page is gone", not "the page misbehaved".

    Playwright has no stable exception class for this across versions, and the same
    condition surfaces as several different messages depending on which call noticed it.
    """
    m = str(e).lower()
    return ("target page, context or browser has been closed" in m
            or "browser has been closed" in m
            or "target closed" in m
            or "connection closed" in m
            or "browser closed" in m)


class Relauncher:
    """Owns a browser context+page pair and can rebuild it after a crash.

    `open_fn` returns a fresh `(context, page)`. `on_restart(where, attempt)` is optional
    and exists so a lane can report the restart to the user rather than only the log.
    """

    def __init__(self, open_fn: Callable[[], tuple[Any, Any]],
                 on_restart: Callable[[str, int], None] | None = None,
                 max_relaunch: int = MAX_RELAUNCH) -> None:
        self._open_fn = open_fn
        self._on_restart = on_restart
        self._max = max_relaunch
        self.attempts = 0
        self.ctx: Any = None
        self.page: Any = None

    @property
    def current(self) -> tuple[Any, Any]:
        return self.ctx, self.page

    def open(self) -> tuple[Any, Any]:
        self.ctx, self.page = self._open_fn()
        return self.current

    def recover(self, where: str) -> bool:
        """Relaunch after a crash. False once the cap is spent — the caller re-raises."""
        if self.attempts >= self._max:
            log.error("browser died during %s and the relaunch cap (%d) is spent", where, self._max)
            return False
        self.attempts += 1
        log.warning("browser died during %s — relaunching (%d/%d)", where, self.attempts, self._max)
        if self._on_restart:
            try:
                self._on_restart(where, self.attempts)
            except Exception:                                     # noqa: BLE001
                log.debug("on_restart callback failed", exc_info=True)
        self.close()
        # Let the dead Chrome release its persistent profile lock before the relaunch
        # attaches to the same user_data_dir — an immediate relaunch can land on the
        # still-exiting process and die again within seconds.
        time.sleep(RELAUNCH_SETTLE_SEC)
        self.open()
        return True

    def close(self) -> None:
        """Best effort — the context is usually already gone, which is why we are here."""
        try:
            if self.ctx is not None:
                self.ctx.close()
        except Exception:                                         # noqa: BLE001
            pass
        self.ctx = self.page = None
