"""W202 (CRM T1464): real CPU %, a small top-processes snapshot, and the SAFE process reaper.

Stdlib only (no psutil): one PowerShell/CIM call on Windows, `ps` on macOS/Linux. Every public
function is best effort — it never raises and caches for `CACHE_SEC` so the 5-min self-check
stays cheap. The parsers are pure functions (tested in tests/test_sysload.py).

What may be stopped automatically (never anything else):
  (a) orphaned agent-owned Chrome — `browser_recovery.reap_orphan_browsers` over our profile dirs
      (a live holder that is not a descendant of THIS agent), skipped while a WhatsApp login is open;
  (b) a second agent python (`-m webscraper agent`) that is not this process, its ancestor or child.
Third-party processes are only stopped when the owner lists their exe name in the CRM setting
`proc_kill__<device>` (comma list, e.g. `OneDrive.exe, Teams.exe`) — default empty = report only.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from typing import Any

log = logging.getLogger(__name__)

CACHE_SEC = 45.0
_cache: dict[str, Any] = {"at": 0.0, "snap": None}
_CORES = os.cpu_count() or 1

#: Windows: total CPU + every process (perf counters, no admin) + command lines of the
#: processes we must classify (chrome / python). Tab-separated, one record per line.
_PS_SNAPSHOT = (
    "$ErrorActionPreference='SilentlyContinue';"
    "$t=(Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter \"Name='_Total'\").PercentProcessorTime;"
    "\"TOTAL`t$t\";"
    "Get-CimInstance Win32_PerfFormattedData_PerfProc_Process | Where-Object { $_.IDProcess -gt 0 } | "
    "ForEach-Object { \"P`t$($_.IDProcess)`t$($_.Name)`t$($_.PercentProcessorTime)`t$($_.WorkingSetPrivate)\" };"
    "Get-CimInstance Win32_Process -Filter \"Name like 'chrome%' or Name like 'python%' or Name like 'pythonw%'\" | "
    "ForEach-Object { \"C`t$($_.ProcessId)`t$($_.ParentProcessId)`t$($_.CommandLine)\" }"
)

_AGENT_PY_RE = re.compile(r"webscraper[\\/\"' ].*\bagent\b|-m\s+webscraper\s+agent", re.I)
_CHROME_RE = re.compile(r"chrom(e|ium)|Google Chrome", re.I)


# -- parsers (pure) ----------------------------------------------------------------------

def parse_typeperf(text: str) -> float | None:
    """`typeperf "\\Processor(_Total)\\% Processor Time" -sc 1` -> the sample as a float."""
    for line in text.splitlines():
        m = re.match(r'\s*"[^"]+"\s*,\s*"([0-9.]+)"\s*$', line)
        if m:
            return round(float(m.group(1)), 1)
    return None


def parse_win_snapshot(text: str, cores: int = _CORES) -> dict:
    """Parse `_PS_SNAPSHOT` output -> {cpu_pct, procs: {pid: {...}}, cmd: {pid: (ppid, cmdline)}}.

    PerfProc `PercentProcessorTime` is per-core (a 4-core box can show 400), so it is divided
    by `cores` to read on the same 0-100 scale as the total. Instance names carry `#n`
    suffixes (`chrome#12`) — stripped."""
    out: dict[str, Any] = {"cpu_pct": None, "procs": {}, "cmd": {}}
    for line in text.splitlines():
        parts = line.rstrip("\r").split("\t")
        try:
            if parts[0] == "TOTAL" and len(parts) > 1 and parts[1].strip():
                out["cpu_pct"] = max(0.0, min(100.0, float(parts[1])))
            elif parts[0] == "P" and len(parts) >= 5:
                pid = int(parts[1])
                name = re.sub(r"#\d+$", "", parts[2].strip())
                if name.lower() in ("_total", "idle"):
                    continue
                cpu = float(parts[3] or 0) / max(1, cores)
                rss = float(parts[4] or 0) / 1e6
                out["procs"][pid] = {"name": name, "pid": pid, "cpu": round(cpu, 1), "rss_mb": round(rss, 1)}
            elif parts[0] == "C" and len(parts) >= 3:
                out["cmd"][int(parts[1])] = (int(parts[2] or 0), "\t".join(parts[3:]).strip())
        except (ValueError, IndexError):
            continue
    return out


def parse_ps(text: str, cores: int = _CORES) -> dict:
    """`ps -axo pid=,ppid=,%cpu=,rss=,command=` -> same shape as `parse_win_snapshot`.
    `ps` %cpu is per-core too, so the machine figure is the sum / cores (capped at 100)."""
    out: dict[str, Any] = {"cpu_pct": None, "procs": {}, "cmd": {}}
    total = 0.0
    for line in text.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid, ppid, cpu, rss = int(parts[0]), int(parts[1]), float(parts[2]), float(parts[3])
        except ValueError:
            continue
        cmd = parts[4].strip()
        exe = cmd.split(" --", 1)[0].split(" -", 1)[0]
        name = os.path.basename(exe.rstrip("/")) or exe
        total += cpu
        out["procs"][pid] = {"name": name[:40], "pid": pid, "cpu": round(cpu / max(1, cores), 1),
                             "rss_mb": round(rss / 1e3, 1)}
        out["cmd"][pid] = (ppid, cmd)
    if out["procs"]:
        out["cpu_pct"] = round(min(100.0, total / max(1, cores)), 1)
    return out


def classify(snap: dict, me: int, data_dir: str) -> set[int]:
    """PIDs that are OURS: this process and its descendants, any agent python, and any Chrome
    whose command line names our data dir (profiles / wa-profiles)."""
    cmd = snap.get("cmd") or {}
    parent = {pid: ppid for pid, (ppid, _) in cmd.items()}
    dd = data_dir.lower().replace("/", "\\") if sys.platform == "win32" else data_dir.lower()
    ours: set[int] = {me}
    for pid, (_, line) in cmd.items():
        low = line.lower()
        if _AGENT_PY_RE.search(line) and "python" in low:
            ours.add(pid)
        elif _CHROME_RE.search(line) and dd and dd in low:
            ours.add(pid)
    for pid in list(cmd):                                    # descendants of anything ours
        seen, p = 0, pid
        while p in parent and seen < 12:
            p = parent[p]
            seen += 1
            if p in ours:
                ours.add(pid)
                break
    return ours


# -- snapshot ------------------------------------------------------------------------------

def _data_dir() -> str:
    try:
        from webscraper.config import settings
        return str(settings.profile_dir.parent)
    except Exception:                                             # noqa: BLE001
        return ""


def _raw_snapshot() -> dict:
    if sys.platform == "win32":
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_SNAPSHOT],
                           capture_output=True, text=True, timeout=40,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        snap = parse_win_snapshot(r.stdout)
        # The CIM `_Total` figure is a coarse integer that often reads 0 on a single query
        # (measured 10/0/0 on a box typeperf put at 2 %) — typeperf is the primary, CIM the fallback.
        try:
            t = subprocess.run(["typeperf", r"\Processor(_Total)\% Processor Time", "-sc", "1"],
                               capture_output=True, text=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            tp = parse_typeperf(t.stdout)
            if tp is not None:
                snap["cpu_pct"] = tp
        except Exception:                                         # noqa: BLE001
            pass
        return snap
    r = subprocess.run(["ps", "-axo", "pid=,ppid=,%cpu=,rss=,command="], capture_output=True,
                       text=True, timeout=10)
    return parse_ps(r.stdout)


def snapshot(force: bool = False) -> dict | None:
    """Cached raw snapshot (None when the listing failed)."""
    now = time.monotonic()
    if not force and _cache["snap"] is not None and now - _cache["at"] < CACHE_SEC:
        return _cache["snap"]
    try:
        snap = _raw_snapshot()
    except Exception:                                             # noqa: BLE001
        log.debug("process snapshot failed", exc_info=True)
        snap = None
    _cache.update(at=now, snap=snap)
    return snap


def load() -> dict:
    """{cpu_pct: real 0-100 % or None, load1: 1-min loadavg or None, cores}."""
    out: dict[str, Any] = {"cpu_pct": None, "load1": None, "cores": _CORES}
    try:
        out["load1"] = round(os.getloadavg()[0], 2)
    except Exception:                                             # noqa: BLE001
        pass
    snap = snapshot()
    if snap and snap.get("cpu_pct") is not None:
        out["cpu_pct"] = round(float(snap["cpu_pct"]), 1)
    return out


_last_killed: list[str] = []


def processes(top: int = 8) -> dict | None:
    """{top_cpu: [...], top_rss: [...], agent_rss_mb, killed} — kept well under 2 KB."""
    snap = snapshot()
    if not snap or not snap.get("procs"):
        return None
    ours = classify(snap, os.getpid(), _data_dir())
    rows = [dict(p, agent=pid in ours) for pid, p in snap["procs"].items()]

    def trim(r: dict) -> dict:
        return {"name": r["name"][:32], "pid": r["pid"], "cpu": r["cpu"], "rss_mb": round(r["rss_mb"]),
                **({"agent": True} if r["agent"] else {})}
    by_cpu = sorted(rows, key=lambda r: r["cpu"] or 0, reverse=True)[:top]
    by_rss = sorted(rows, key=lambda r: r["rss_mb"] or 0, reverse=True)[:top]
    return {"top_cpu": [trim(r) for r in by_cpu], "top_rss": [trim(r) for r in by_rss],
            "agent_rss_mb": round(sum(r["rss_mb"] or 0 for r in rows if r["agent"])),
            "count": len(rows), "killed": list(_last_killed[-6:])}


# -- reaper ----------------------------------------------------------------------------------

def _approved_names() -> set[str]:
    try:
        from webscraper.agent import DEVICE_NAME
    except Exception:                                             # noqa: BLE001
        DEVICE_NAME = os.environ.get("HVT_AGENT_DEVICE", "")
    raw = (os.environ.get(f"PROC_KILL__{DEVICE_NAME.upper()}") if DEVICE_NAME else "") or ""
    return {n.strip().lower().removesuffix(".exe") for n in raw.split(",") if n.strip()}


def _kill_pid(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                               timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return r.returncode == 0
        os.kill(pid, 9)
        return True
    except Exception:                                             # noqa: BLE001
        return False


def _ancestors(snap: dict, pid: int) -> set[int]:
    parent = {p: pp for p, (pp, _) in (snap.get("cmd") or {}).items()}
    out, p = set(), pid
    for _ in range(12):
        p = parent.get(p)
        if not p:
            break
        out.add(p)
    return out


def reap(reason: str = "periodic") -> list[str]:
    """Stop the SAFE set (module doc). Returns 'name pid (why)' per kill; every kill is logged.
    Never raises."""
    killed: list[str] = []
    try:
        from webscraper import wa_verify
        login_open = wa_verify.login_in_progress()
    except Exception:                                             # noqa: BLE001
        login_open = True                                          # unsure -> never touch Chrome
    try:
        if not login_open:
            from webscraper.agent import known_profile_dirs
            from webscraper.browser_recovery import reap_orphan_browsers
            n = reap_orphan_browsers(known_profile_dirs(), f"W202 {reason}")
            if n:
                killed.append(f"{n} orphan Chrome (profile not held by this agent)")
                log.warning("W202: stopped %d orphan agent Chrome(s) [%s]", n, reason)
    except Exception:                                             # noqa: BLE001
        log.debug("orphan chrome reap failed", exc_info=True)
    snap = snapshot(force=True)
    if not snap:
        return killed
    me = os.getpid()
    mine = _ancestors(snap, me) | {me}
    cmd = snap.get("cmd") or {}
    for pid, (ppid, line) in cmd.items():                          # (b) a second agent python
        if pid in mine or ppid == me or "python" not in line.lower() or not _AGENT_PY_RE.search(line):
            continue
        if " doctor" in line or " serve" in line:
            continue
        if me in _ancestors(snap, pid):                            # our own child
            continue
        if _kill_pid(pid):
            killed.append(f"python {pid} (second agent instance)")
            log.warning("W202: stopped a second agent instance pid %d: %s [%s]", pid, line[:160], reason)
    approved = _approved_names()
    if approved:                                                   # owner-approved third-party exes
        ours = classify(snap, me, _data_dir())
        for pid, p in (snap.get("procs") or {}).items():
            nm = p["name"].lower().removesuffix(".exe")
            if nm in approved and pid not in ours and pid not in mine and _kill_pid(pid):
                killed.append(f"{p['name']} {pid} (approved in proc_kill)")
                log.warning("W202: stopped %s pid %d (cpu %s%%, %s MB) — listed in proc_kill [%s]",
                            p["name"], pid, p["cpu"], p["rss_mb"], reason)
    if killed:
        stamp = time.strftime("%H:%M")
        _last_killed.extend(f"{stamp} {k}" for k in killed)
        del _last_killed[:-10]
        _cache["at"] = 0.0                                         # next report re-reads
    return killed
