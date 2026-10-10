"""W183 (CRM T1101, 2026-10-11): stray sweep matching, Job Object pure parts, low-RAM admission,
pressure shedding decision."""
from __future__ import annotations

import ctypes
import subprocess
import sys

import pytest

from webscraper import browser_recovery as br
from webscraper import job_objects as jo
from webscraper import server as server_mod

ROOT = r"D:\5 - Repositories\Hv Technologies\web-scraper"
ME = 100


# -- stray matching -------------------------------------------------------------------------------

def test_stray_kind_driver_chrome_and_never_the_users_chrome():
    drv = (rf'"{ROOT}\.venv\Lib\site-packages\playwright\driver\node.exe" '
           rf'"{ROOT}\.venv\Lib\site-packages\playwright\driver\package\cli.js" run-driver')
    assert br.stray_kind(drv, ROOT) == "driver"
    ours = rf'"C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir="{ROOT}\data\browser-profile-open" --flag'
    assert br.stray_kind(ours, ROOT) == "chrome"
    assert br.stray_kind(rf"chrome.exe --type=renderer --user-data-dir={ROOT}\data\wa-profiles\main x", ROOT) == "chrome"
    # the user's own Chrome / another checkout / our python never match
    assert br.stray_kind(r'"C:\Program Files\Google\Chrome\Application\chrome.exe" --profile-directory=Default', ROOT) is None
    assert br.stray_kind(r"chrome.exe --user-data-dir=D:\other\web-scraper\data\browser-profile", ROOT) is None
    assert br.stray_kind(rf"{ROOT}\.venv\Scripts\python.exe -m webscraper agent", ROOT) is None
    assert br.stray_kind(r"node.exe C:\Users\x\AppData\Roaming\npm\node_modules\playwright\cli.js run-driver", ROOT) is None


def test_stray_kind_posix_paths():
    root = "/Users/hv/web-scraper"
    assert br.stray_kind(f"/Users/hv/web-scraper/.venv/lib/python3.13/site-packages/playwright/driver/node "
                         f"/Users/hv/web-scraper/.venv/lib/python3.13/site-packages/playwright/driver/package/cli.js run-driver",
                         root) == "driver"
    assert br.stray_kind(f"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir={root}/data/wa-main",
                         root) == "chrome"


def test_stray_pids_skips_what_a_live_webscraper_owns():
    chrome = rf"chrome.exe --user-data-dir={ROOT}\data\browser-profile-open"
    driver = rf"{ROOT}\.venv\node.exe {ROOT}\.venv\playwright\driver\package\cli.js run-driver"
    table = [
        (ME, 1, rf"{ROOT}\.venv\Scripts\python.exe -m webscraper agent"),
        (200, ME, driver), (201, 200, chrome), (202, 201, chrome),          # ours, live
        (300, 9999, driver), (301, 300, chrome), (302, 301, chrome),        # previous agent's leftovers
        (400, 1, rf"{ROOT}\.venv\Scripts\python.exe -m webscraper wa-login main"),
        (401, 400, driver), (402, 401, chrome),                             # a manual wa-login: owned
        (500, 1, r"chrome.exe --profile-directory=Default"),                # the user's Chrome
    ]
    got = br.stray_pids(table, ME, ROOT)
    assert sorted(got["driver"]) == [300]
    assert sorted(got["chrome"]) == [301, 302]


def test_descends_from_handles_cycles_and_missing():
    assert br.descends_from(3, 5, {3: 2, 2: 5}) is True
    assert br.descends_from(3, 7, {3: 2, 2: 3}) is False
    assert br.descends_from(3, 7, {}) is False


# -- Job Object pure parts ------------------------------------------------------------------------

def test_cap_mb_default_override_off():
    assert jo.job_mem_cap_mb(8000, None) == 2400
    assert jo.job_mem_cap_mb(16000, "") == 4800
    assert jo.job_mem_cap_mb(3000, None) == 1200          # floor
    assert jo.job_mem_cap_mb(None, None) == 1200
    assert jo.job_mem_cap_mb(8000, "1500") == 1500
    assert jo.job_mem_cap_mb(8000, "0") == 0
    assert jo.job_mem_cap_mb(8000, "junk") == 2400


def test_limit_struct():
    info = jo.build_limit_info(1500)
    assert info.BasicLimitInformation.LimitFlags == jo.JOB_OBJECT_LIMIT_JOB_MEMORY
    assert not info.BasicLimitInformation.LimitFlags & jo.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert info.JobMemoryLimit == 1500 * 1024 * 1024
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(info) == 144                  # Windows x64 JOBOBJECT_EXTENDED_LIMIT_INFORMATION


def test_cap_hit_ratio():
    assert jo.cap_hit(98, 100) and not jo.cap_hit(50, 100) and not jo.cap_hit(10, 0)


def test_cap_browser_is_a_noop_off_windows(monkeypatch):
    monkeypatch.setattr(jo.sys, "platform", "darwin")
    monkeypatch.setattr(jo, "configured_cap_mb", lambda: pytest.fail("must not run off Windows"))
    jo.cap_browser("/tmp/x", "Maps")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")
def test_job_memory_cap_kills_a_real_process():
    """Smoke: a python child in a 50 MB job dies when it allocates 200 MB."""
    exe = getattr(sys, "_base_executable", None) or sys.executable   # venv python.exe is a launcher
    p = subprocess.Popen([exe, "-c",
                          "import time; time.sleep(1.5); b = bytearray(200*1024*1024); print('alive')"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    job = jo.create_job(50)
    assert job
    try:
        assert jo.assign_pid(job, p.pid) == 0
        out, err = p.communicate(timeout=30)
        assert p.returncode != 0 and "alive" not in out
        assert "MemoryError" in err or p.returncode != 0
        peak, limit = jo.job_peak(job)
        assert limit == 50 * 1024 * 1024
    finally:
        jo.close_job(job)
        if p.poll() is None:
            p.kill()


# -- admission + shedding -------------------------------------------------------------------------

def test_start_limit_lowmem_default_and_explicit_wins():
    assert server_mod.start_limit_pct(None, 8000) == 75.0
    assert server_mod.start_limit_pct("", 8192) == 75.0
    assert server_mod.start_limit_pct(None, 16000) == 85.0
    assert server_mod.start_limit_pct(None, None) == 85.0
    assert server_mod.start_limit_pct("85", 8000) == 85.0
    assert server_mod.start_limit_pct("bad", 8000) == 75.0


def test_shed_step_recycle_then_park_newest_never_last():
    started = [(1, "discovery"), (2, "enrichment"), (3, "discovery")]
    assert server_mod.shed_step(False, started) == ("recycle", None)
    assert server_mod.shed_step(True, started) == ("park", 3)
    assert server_mod.shed_step(True, [(1, "discovery")]) == ("none", None)
    assert server_mod.shed_step(False, []) == ("none", None)


def test_relauncher_recycle_request():
    rl = br.Relauncher(lambda: (object(), None))
    assert rl.recycle_requested() is False
    br.request_recycle("test")
    assert rl.recycle_requested() is True
