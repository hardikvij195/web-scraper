"""W183 (CRM T1101, 2026-10-11): a hard RAM cap per Chrome tree on Windows — Job Objects via ctypes.

"3 - ASUS" (8 GB) froze about every 40 min with the whole box at 70-84 %+ RAM: one Chrome tree (Maps
tab, a WhatsApp account, the enrichment fallback) grew until Windows paged everything, and the agent
could not even run its own watchdog. Every one of OUR Chrome launches now goes into a Windows Job
Object with `JOB_OBJECT_LIMIT_JOB_MEMORY` (committed memory of the whole tree). When the tree hits the
cap, Chrome's own allocations fail and it dies — the existing crash path (`Relauncher.recover`,
`launch_evicting`, the W180 page-crash path, the WhatsApp loop's relaunch) brings it back, and the
next `cap_browser()` on that profile logs "Chrome hit its RAM cap (N MB) — relaunching".

Decisions:
- `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` is OFF: the job handle lives in this process; with the flag on,
  an agent crash / watchdog `os._exit` would kill a WhatsApp Chrome mid-sync. Orphans are reaped by
  `browser_recovery.reap_strays()` at the next start instead.
- Nested jobs: libuv (Playwright's node driver) already puts Chrome in its own job; Windows 8+ allows a
  process in several nested jobs, so the assign normally succeeds. Any failure logs ONE warning per
  browser kind and never raises.
- The root Chrome and the children that already exist are assigned (enumerated by the profile dir on
  their command line, `profile_holder_pids`); children spawned later inherit the job.
- Knob `CHROME_JOB_MEM_MB` (per device `CHROME_JOB_MEM_MB__<DEVICE>`, the CRM `lead_gen_settings`
  pattern); default max(1200, 30 % of total RAM); 0 = off. Non-Windows = no-op.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("webscraper.job_objects")

JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001

DEFAULT_MIN_MB = 1200
DEFAULT_RAM_FRACTION = 0.30
#: a second cap request for the same profile within this window is the same launch seen twice
#: (`launch_evicting` + the launcher's own hook) — skipped.
DEBOUNCE_SEC = 10.0
#: let Chrome spawn its first children before the profile-dir enumeration.
ASSIGN_DELAY_SEC = 1.5
#: peak / cap at or above this = the tree ran into the cap (Chrome dies on the failed allocation).
CAP_HIT_RATIO = 0.95


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount",
                                               "OtherOperationCount", "ReadTransferCount",
                                               "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def build_limit_info(cap_mb: int) -> JOBOBJECT_EXTENDED_LIMIT_INFORMATION:
    """Pure: the struct `SetInformationJobObject(class 9)` gets — job memory limit only, no
    kill-on-close (see the module docstring)."""
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_JOB_MEMORY
    info.JobMemoryLimit = int(cap_mb) * 1024 * 1024
    return info


def job_mem_cap_mb(total_mb: float | None, raw: str | None) -> int:
    """Pure: the cap in MB. `raw` = the knob (None/blank = default); 0 / negative = off;
    garbage = default. Default = max(1200, 30 % of total RAM), 1200 when RAM is unknown."""
    default = max(DEFAULT_MIN_MB, int((total_mb or 0) * DEFAULT_RAM_FRACTION))
    if raw is None or not str(raw).strip():
        return default
    try:
        v = int(float(str(raw).strip()))
    except ValueError:
        return default
    return max(0, v)


def cap_hit(peak_bytes: int, limit_bytes: int) -> bool:
    """Pure: did a job's tree run into its limit?"""
    return limit_bytes > 0 and peak_bytes >= limit_bytes * CAP_HIT_RATIO


def configured_cap_mb() -> int:
    raw = None
    try:
        from webscraper.lanes import _device_env
        raw = _device_env("CHROME_JOB_MEM_MB")
    except Exception:                                             # noqa: BLE001
        raw = None
    if raw is None or not str(raw).strip():
        raw = os.getenv("CHROME_JOB_MEM_MB")
    total = None
    try:
        from webscraper import healthcheck
        total = healthcheck._memory().get("total_mb")
    except Exception:                                             # noqa: BLE001
        pass
    return job_mem_cap_mb(total, raw)


# -- Windows side -------------------------------------------------------------------------------

_LOCK = threading.Lock()
#: profile key -> (job HANDLE, cap MB). Kept open for the browser's lifetime (closing it with no
#: kill-on-close only drops our handle; the job lives while a process is in it).
_JOBS: dict[str, tuple[int, int]] = {}
_LAST_REQ: dict[str, float] = {}
_WARNED: set[str] = set()


def _k32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)          # type: ignore[attr-defined]
    H = ctypes.c_void_p
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    k.CreateJobObjectW.restype = H
    k.SetInformationJobObject.argtypes = [H, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    k.SetInformationJobObject.restype = ctypes.c_int
    k.QueryInformationJobObject.argtypes = [H, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    k.QueryInformationJobObject.restype = ctypes.c_int
    k.AssignProcessToJobObject.argtypes = [H, H]
    k.AssignProcessToJobObject.restype = ctypes.c_int
    k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k.OpenProcess.restype = H
    k.CloseHandle.argtypes = [H]
    k.CloseHandle.restype = ctypes.c_int
    return k


def _warn_once(kind: str, msg: str, *args) -> None:
    if kind in _WARNED:
        log.debug(msg, *args)
        return
    _WARNED.add(kind)
    log.warning(msg + " (W183; logged once per browser kind)", *args)


def create_job(cap_mb: int) -> int | None:
    """win32: a new job with the memory cap. None (and a warning) on failure."""
    k = _k32()
    h = k.CreateJobObjectW(None, None)
    if not h:
        _warn_once("create", "CreateJobObject failed (error %d)", ctypes.get_last_error())
        return None
    info = build_limit_info(cap_mb)
    if not k.SetInformationJobObject(h, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                                     ctypes.byref(info), ctypes.sizeof(info)):
        _warn_once("limit", "SetInformationJobObject failed (error %d)", ctypes.get_last_error())
        k.CloseHandle(h)
        return None
    return int(h)


def assign_pid(job: int, pid: int) -> int:
    """win32: put `pid` in `job`. 0 = ok, else the Windows error code."""
    k = _k32()
    hp = k.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
    if not hp:
        return ctypes.get_last_error() or -1
    try:
        return 0 if k.AssignProcessToJobObject(job, hp) else (ctypes.get_last_error() or -1)
    finally:
        k.CloseHandle(hp)


def job_peak(job: int) -> tuple[int, int]:
    """win32: (PeakJobMemoryUsed, JobMemoryLimit) in bytes; (0, 0) when the query fails."""
    k = _k32()
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    if not k.QueryInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                                       ctypes.byref(info), ctypes.sizeof(info), None):
        return 0, 0
    return int(info.PeakJobMemoryUsed), int(info.JobMemoryLimit)


def close_job(job: int) -> None:
    try:
        _k32().CloseHandle(job)
    except Exception:                                             # noqa: BLE001
        pass


def _assign_profile(profile_dir: Path, what: str, cap_mb: int) -> None:
    from webscraper.browser_recovery import _list_processes, profile_holder_pids
    key = str(profile_dir).lower()
    with _LOCK:
        old = _JOBS.pop(key, None)
    if old is not None:
        peak, limit = job_peak(old[0])
        if cap_hit(peak, limit):
            log.warning("Chrome hit its RAM cap (%d MB) — relaunching [%s, profile %s] (W183)",
                        old[1], what or "browser", profile_dir.name)
        close_job(old[0])
    pids = profile_holder_pids(profile_dir, _list_processes())
    if not pids:
        log.debug("W183: no Chrome found on %s to cap", profile_dir.name)
        return
    job = create_job(cap_mb)
    if job is None:
        return
    ok, errs = 0, []
    for pid in pids:
        err = assign_pid(job, pid)
        if err:
            errs.append((pid, err))
        else:
            ok += 1
    if not ok:
        _warn_once(what or "browser", "could not put %s Chrome (%s) into a %d MB job: %s",
                   what or "browser", profile_dir.name, cap_mb, errs[:3])
        close_job(job)
        return
    with _LOCK:
        _JOBS[key] = (job, cap_mb)
    log.info("W183: %s Chrome on %s capped at %d MB (%d process(es) assigned%s)", what or "browser",
             profile_dir.name, cap_mb, ok, f", {len(errs)} failed" if errs else "")


def cap_browser(profile_dir: Path | str | None, what: str = "") -> None:
    """Call right after a Chrome launch on `profile_dir`. Windows only; never raises; does the
    process enumeration (a PowerShell CIM call, ~1 s) on a daemon thread so the launch is not slowed."""
    if sys.platform != "win32" or not profile_dir:
        return
    try:
        cap = configured_cap_mb()
        if cap <= 0:
            return
        p = Path(profile_dir)
        key = str(p).lower()
        now = time.monotonic()
        with _LOCK:
            if now - _LAST_REQ.get(key, -1e9) < DEBOUNCE_SEC:
                return
            _LAST_REQ[key] = now

        def _run() -> None:
            try:
                time.sleep(ASSIGN_DELAY_SEC)
                _assign_profile(p, what, cap)
            except Exception as e:                                # noqa: BLE001
                _warn_once(what or "browser", "Chrome RAM cap failed: %s", e)
        threading.Thread(target=_run, name=f"w183-cap-{p.name}", daemon=True).start()
    except Exception as e:                                        # noqa: BLE001
        _warn_once(what or "browser", "Chrome RAM cap failed: %s", e)
