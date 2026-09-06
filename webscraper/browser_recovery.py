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

import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger("webscraper.browser_recovery")

#: A browser that cannot start at all must fail fast rather than loop.
MAX_RELAUNCH = 3
#: Seconds to wait between closing the dead context and launching a fresh one.
RELAUNCH_SETTLE_SEC = 2.0

#: T397 (2026-09-06, the Mac's dozen `about:blank` tabs + "Restore pages?"). Chrome flags
#: that stop a persistent profile from ever offering session restore. The old
#: `--disable-session-crashed-bubble` was removed from Chrome years ago and did nothing;
#: `--hide-crash-restore-bubble` is the live switch. Kept both — unknown flags are ignored.
RESTORE_BUBBLE_ARGS = ["--hide-crash-restore-bubble", "--disable-session-crashed-bubble",
                       "--no-first-run", "--no-default-browser-check"]

#: Chrome's own words for "another Chrome already owns this user-data-dir". When a launch
#: dies with one of these, the running instance has just been handed our `about:blank`
#: start URL as a NEW TAB (its process-singleton IPC) — that is where the tabs came from.
_PROFILE_BUSY = ("opening in existing browser session", "processsingleton",
                 "profile is already in use", "profile directory is in use",
                 "already in use by another instance")


def is_profile_busy(e: BaseException | str) -> bool:
    m = str(e).lower()
    return any(s in m for s in _PROFILE_BUSY)


def mark_profile_clean(profile_dir: Path) -> None:
    """Rewrite the profile's exit state so Chrome never shows "Restore pages? Chrome didn't
    shut down correctly" — an agent restart (`os._exit`) kills its Chromes hard, so every
    profile on this machine reads as crashed by the next launch. Best effort."""
    try:
        d = Path(profile_dir) / "Default"
        d.mkdir(parents=True, exist_ok=True)
        pf = d / "Preferences"
        prefs: dict[str, Any] = {}
        if pf.exists():
            try:
                prefs = json.loads(pf.read_text(encoding="utf-8") or "{}")
            except (ValueError, OSError):
                prefs = {}
        prefs.setdefault("profile", {})["exit_type"] = "Normal"
        prefs["profile"]["exited_cleanly"] = True
        prefs.setdefault("session", {})["restore_on_startup"] = 5      # 5 = new tab page
        pf.write_text(json.dumps(prefs), encoding="utf-8")
    except OSError as e:                                          # noqa: BLE001
        log.debug("could not mark %s clean: %s", profile_dir, e)


def close_blank_pages(ctx: Any, keep: Any = None) -> int:
    """Close every `about:blank` tab in `ctx` except `keep` (the working page). A Chrome
    that received other launches' start URLs through its process singleton accumulates
    one blank tab per collision; a headed run shows them all to the user."""
    n = 0
    try:
        pages = list(getattr(ctx, "pages", []) or [])
    except Exception:                                             # noqa: BLE001
        return 0
    for p in pages:
        if p is keep:
            continue
        try:
            if (p.url or "about:blank") in ("about:blank", ""):
                p.close()
                n += 1
        except Exception:                                         # noqa: BLE001
            pass
    if n:
        log.info("closed %d blank tab(s)", n)
    return n


# -- profile lock / orphan Chrome handling -----------------------------------------
_LOCK_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile")


def lock_holder_pid(profile_dir: Path) -> int | None:
    """PID of the Chrome holding `profile_dir` (from its `SingletonLock` symlink,
    `<host>-<pid>`); None on Windows (no symlink) or when there is no lock."""
    try:
        target = os.readlink(str(Path(profile_dir) / "SingletonLock"))
    except OSError:
        return None
    tail = target.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _parent_pid(pid: int) -> int | None:
    if sys.platform == "win32":
        return None
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        return int(out) if out.isdigit() else None
    except (OSError, subprocess.SubprocessError):
        return None


def _is_descendant_of(pid: int, ancestor: int, max_depth: int = 12) -> bool:
    """True when `ancestor` is `pid` itself or somewhere up its parent chain. A Chrome we
    launched hangs off Playwright's node driver, which hangs off THIS process; a Chrome
    from a previous agent process hangs off a driver whose parent is gone (launchd/init)."""
    cur = pid
    for _ in range(max_depth):
        if cur == ancestor:
            return True
        parent = _parent_pid(cur)
        if parent is None or parent <= 1:
            return False
        cur = parent
    return False


def _remove_lock_files(profile_dir: Path) -> None:
    for name in _LOCK_FILES:
        try:
            (Path(profile_dir) / name).unlink()
        except OSError:
            pass


def kill_profile_holder(profile_dir: Path, reason: str = "") -> bool:
    """Kill whatever Chrome owns `profile_dir` and clear its lock. Only OUR profiles are
    ever passed here, so the user's own Chrome (its default profile) is never touched.
    Returns True when a process was killed."""
    profile_dir = Path(profile_dir)
    killed = False
    pid = lock_holder_pid(profile_dir)
    if pid and pid_alive(pid):
        log.warning("killing Chrome pid %d holding %s%s", pid, profile_dir.name,
                    f" ({reason})" if reason else "")
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(30):
                if not pid_alive(pid):
                    break
                time.sleep(0.1)
            if pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
            killed = True
        except OSError as e:
            log.debug("kill %d failed: %s", pid, e)
    # Children and helpers do not hold the lock; sweep by command line (best effort).
    # Anchored on the dir's END: `browser-profile` must never match `browser-profile-open`.
    try:
        if sys.platform == "win32":
            d = str(profile_dir).replace("\\", "\\\\")
            subprocess.run(["wmic", "process", "where",
                            f"CommandLine like '%--user-data-dir={d}\"%' or CommandLine like '%--user-data-dir={d} %'",
                            "call", "terminate"], capture_output=True, timeout=15)
        else:
            import re as _re
            needle = "--user-data-dir=" + _re.escape(str(profile_dir)) + "( |$)"
            r = subprocess.run(["pkill", "-9", "-f", "--", needle], capture_output=True, timeout=10)
            killed = killed or r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        pass
    _remove_lock_files(profile_dir)
    return killed


def reap_orphan_browsers(profile_dirs: Iterable[Path], reason: str = "agent start") -> int:
    """For each of OUR profile dirs: a lock whose PID is dead is cleared; a live holder
    that is not descended from THIS process (the agent that started it exited with
    `os._exit`, leaving Chrome and its Playwright driver behind) is an orphan and is
    killed. One agent per machine, so nothing else may legitimately own these dirs.
    Returns the number killed."""
    n = 0
    me = os.getpid()
    for d in profile_dirs:
        d = Path(d)
        if not d.exists():
            continue
        pid = lock_holder_pid(d)
        if pid is None:
            continue
        if not pid_alive(pid):
            log.info("clearing stale lock on %s (pid %d is gone)", d.name, pid)
            _remove_lock_files(d)
            continue
        if not _is_descendant_of(pid, me):
            if kill_profile_holder(d, f"orphan, {reason}"):
                n += 1
    return n


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
                 max_relaunch: int = MAX_RELAUNCH,
                 profile_dir: Path | None = None) -> None:
        self._open_fn = open_fn
        self._on_restart = on_restart
        self._max = max_relaunch
        #: T397: the persistent profile this browser owns, so a relaunch can evict a
        #: half-dead Chrome still holding its lock instead of colliding with it.
        self._profile_dir = Path(profile_dir) if profile_dir else None
        self.attempts = 0
        self.ctx: Any = None
        self.page: Any = None

    @property
    def current(self) -> tuple[Any, Any]:
        return self.ctx, self.page

    def open(self) -> tuple[Any, Any]:
        try:
            self.ctx, self.page = self._open_fn()
        except Exception as e:                                    # noqa: BLE001
            # T397: "Opening in existing browser session" = an orphan Chrome owns our
            # profile and just swallowed this launch as a blank tab. Kill it, try once more.
            if self._profile_dir is None or not is_profile_busy(e):
                raise
            log.warning("profile %s is held by another Chrome — evicting it and relaunching",
                         self._profile_dir.name)
            kill_profile_holder(self._profile_dir, "profile busy on launch")
            time.sleep(RELAUNCH_SETTLE_SEC)
            self.ctx, self.page = self._open_fn()
        close_blank_pages(self.ctx, keep=self.page)
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
        # T397: a "dead" context whose Chrome process is actually still up (hung renderer,
        # lost driver pipe) keeps the profile lock; the relaunch would then be swallowed as
        # a blank tab in that zombie. Evict it first.
        if self._profile_dir is not None:
            kill_profile_holder(self._profile_dir, f"relaunch after {where}")
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
