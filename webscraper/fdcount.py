"""W102 (CRM T545): how many file descriptors this process holds, and how many it may.

macOS caps a process at 256 open files by default (`ulimit -n`); Windows has no such cap.
Every Playwright sync session is a driver process (two pipes) plus its own asyncio loop
(kqueue + self-pipe on macOS), so a leaked session costs ~5 descriptors and a slow leak
that is invisible on the four Windows agents killed the Mac's WhatsApp lane after ~2 h
(job #6619, 2026-09-10 08:09 UTC: every slice `[Errno 24] Too many open files`).

Cheap, never raises: `fd_status()` is logged at every WhatsApp batch boundary and when a
verify slice ends, so the trend is in agent.log / the job log before the cap is hit.
"""
from __future__ import annotations

import os
import sys

#: What the agent asks for at start on POSIX (`raise_fd_limit`). Four WhatsApp Chromes,
#: a Maps Chrome, an enrichment Chrome and the sqlite/httpx baseline fit in a few hundred;
#: 4096 leaves room for a leak to show in the log before it becomes an outage.
TARGET_SOFT_LIMIT = 4096
#: Below this the healthcheck warns (the macOS default, 256, is the incident).
WARN_BELOW = 1024


def fd_count() -> int | None:
    """Open descriptors (POSIX) or handles (Windows) held by this process; None if unknown."""
    try:
        import psutil  # optional — present on the dev box, not in requirements
        p = psutil.Process()
        return int(p.num_handles() if sys.platform == "win32" else p.num_fds())
    except Exception:                                             # noqa: BLE001
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            n = wintypes.DWORD()
            if k32.GetProcessHandleCount(k32.GetCurrentProcess(), ctypes.byref(n)):
                return int(n.value)
        except Exception:                                         # noqa: BLE001
            pass
        return None
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        pass
    # macOS has no /proc: probe every slot up to the soft limit (a few thousand fstat
    # calls at most — microseconds each).
    lim = fd_limit()
    top = min(lim[0] if lim else 4096, 65536)
    n = 0
    for fd in range(top):
        try:
            os.fstat(fd)
            n += 1
        except OSError:
            pass
    return n


def fd_limit() -> tuple[int, int] | None:
    """(soft, hard) RLIMIT_NOFILE on POSIX; None on Windows or when unreadable."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        return int(soft), int(hard)
    except Exception:                                             # noqa: BLE001
        return None


def fd_status() -> str:
    """One token for a log line: `fd=123/256` on POSIX, `fd=123` (handles) on Windows."""
    n = fd_count()
    lim = fd_limit()
    if n is None:
        return "fd=?"
    if lim is None:
        return f"fd={n}"
    soft = lim[0]
    return f"fd={n}/{'inf' if soft < 0 else soft}"


def raise_fd_limit(target: int = TARGET_SOFT_LIMIT) -> tuple[int, int] | None:
    """Raise the soft RLIMIT_NOFILE to min(target, hard). Returns (old, new) on POSIX when
    the limit was readable, None on Windows. Never raises."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = target if hard == resource.RLIM_INFINITY or hard < 0 else min(target, hard)
        if soft != resource.RLIM_INFINITY and 0 <= soft < want:
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
            except (ValueError, OSError):
                return soft, soft
            return soft, want
        return soft, soft
    except Exception:                                             # noqa: BLE001
        return None


#: W105 (CRM T553): the launchd job the Mac installer writes. A plist installed before W102
#: has no SoftResourceLimits key, so every login started the agent at 256 again until
#: someone re-ran scripts/install-agent-autostart-mac.sh on the Mac by hand.
LAUNCHD_LABEL = "app.hvtechnologies.leadfinder-agent"
LAUNCHD_LIMIT_XML = (
    "  <key>SoftResourceLimits</key><dict><key>NumberOfFiles</key><integer>%d</integer></dict>\n"
)


def ensure_launchd_limit(plist_path: "str | os.PathLike[str] | None" = None,
                         target: int = TARGET_SOFT_LIMIT) -> str:
    """Self-repair the Mac launchd plist so the NEXT login also starts with `target` files.

    Writes the SoftResourceLimits key into the plist file when it is missing — file only,
    no launchctl: booting the job out from inside would kill this very process, and the
    running one already raised its own limit (`raise_fd_limit`). launchd reads the file on
    the next bootstrap (login / reboot / the installer). Returns 'added' | 'present' |
    'absent' (no plist — autostart never installed) | 'error'. Never raises.
    """
    try:
        if plist_path is None:
            if sys.platform != "darwin":
                return "absent"
            plist_path = os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist")
        path = os.fspath(plist_path)
        if not os.path.isfile(path):
            return "absent"
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if "SoftResourceLimits" in text:
            return "present"
        marker = "</dict></plist>"
        idx = text.rfind(marker)
        if idx < 0:
            return "error"
        text = text[:idx] + (LAUNCHD_LIMIT_XML % int(target)) + text[idx:]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return "added"
    except Exception:  # noqa: BLE001
        return "error"
