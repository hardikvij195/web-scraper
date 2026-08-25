"""Agent self-check: is this machine able to run every Lead Finder lane?

Sent to the CRM on the heartbeat (`lead_gen_agents.checks`) so the Setup tab can show,
per device, what is fine and what is pending — instead of the user learning it from a
lane ending `wa_not_logged_in` or a Chrome-less enrichment hours later. Also printed by
`python -m webscraper doctor`. Every check is cheap (no network, no browser launch) and
never raises: a broken check reports itself as failed with the exception text.
"""
from __future__ import annotations

import importlib
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from .config import settings

#: Bumped by hand when the agent protocol changes; the CRM shows it per device.
AGENT_VERSION = "2026-08-25"

#: Where the Windows scheduled task / macOS launchd job live (see scripts/).
_LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / "app.hvtechnologies.leadfinder-agent.plist"
_WIN_TASK = "HVT Lead Finder Agent"


def _check(ok: bool, detail: str, fix: str = "", optional: bool = False) -> dict:
    c = {"ok": bool(ok), "detail": detail[:200], "fix": ("" if ok else fix)[:200]}
    if optional:
        c["optional"] = True
    return c


def _python() -> dict:
    v = sys.version_info
    return _check((v.major, v.minor) >= (3, 11), f"Python {v.major}.{v.minor}.{v.micro}",
                  "Install Python 3.11+ and reinstall the agent")


def _module(name: str, fix: str, optional: bool = False) -> dict:
    try:
        m = importlib.import_module(name)
        ver = getattr(m, "__version__", "")
        return _check(True, f"{name} {ver}".strip(), optional=optional)
    except Exception as e:  # noqa: BLE001
        return _check(False, f"{name} missing ({type(e).__name__})", fix, optional=optional)


def _playwright_browser() -> dict:
    """Bundled Chromium present? Playwright keeps it under its own cache dir."""
    env = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    sysname = platform.system()
    if env:
        base = Path(env)
    elif sysname == "Windows":
        base = Path(os.getenv("LOCALAPPDATA", "")) / "ms-playwright"
    elif sysname == "Darwin":
        base = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        base = Path.home() / ".cache" / "ms-playwright"
    found = sorted(p.name for p in base.glob("chromium*")) if base.exists() else []
    return _check(bool(found), f"chromium: {', '.join(found) or 'not installed'} ({base})",
                  "python -m playwright install chromium")


def chrome_path() -> str | None:
    """The machine's real Google Chrome, which browser_fetch prefers over bundled Chromium."""
    sysname = platform.system()
    cands: list[str] = []
    if sysname == "Windows":
        for root in (os.getenv("PROGRAMFILES"), os.getenv("PROGRAMFILES(X86)"), os.getenv("LOCALAPPDATA")):
            if root:
                cands.append(str(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    elif sysname == "Darwin":
        cands.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        cands.append(str(Path.home() / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"))
    else:
        for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            p = shutil.which(n)
            if p:
                cands.append(p)
    for c in cands:
        if Path(c).exists():
            return c
    return None


def _real_chrome() -> dict:
    p = chrome_path()
    return _check(bool(p), p or "Google Chrome not found - bundled Chromium will be used (more WAF blocks)",
                  "Install Google Chrome from google.com/chrome", optional=True)


def _wa_session() -> dict:
    d = settings.wa_profiles_dir
    accounts = sorted(p.name for p in d.iterdir() if p.is_dir()) if d.exists() else []
    # A profile that completed wa-login has a Default/ dir with WhatsApp Web's storage.
    live = [a for a in accounts if (d / a / "Default").exists()]
    # discovery + enrichment run without it; only the WhatsApp lane needs it
    return _check(bool(live), f"WhatsApp accounts: {', '.join(live) or 'none'}",
                  "python -m webscraper wa-login <label>  (scan the QR once on this machine)", optional=True)


def _ai_keys() -> dict:
    have = [k for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY") if os.getenv(k)]
    # research step degrades without it
    return _check(bool(have), f"AI keys: {', '.join(have) or 'none'}",
                  "Set a Gemini/Groq key on the CRM Lead Finder Setup tab (pushed to the agent on start)",
                  optional=True)


def _crm_token() -> dict:
    has = bool(os.getenv("CRM_AGENT_TOKEN"))
    return _check(has, "CRM_AGENT_TOKEN set" if has else "CRM_AGENT_TOKEN missing in .env",
                  "Put the wsk_ token from the CRM Setup tab into .env")


def _disk() -> dict:
    try:
        u = shutil.disk_usage(str(settings.profile_dir.parent.parent))
        free_gb = u.free / 1e9
        return _check(free_gb >= 2, f"{free_gb:.1f} GB free",
                      "Free up disk space (browser profiles + leads.db need room)")
    except Exception as e:  # noqa: BLE001
        return _check(False, f"disk check failed: {e}")


def _autostart() -> dict:
    sysname = platform.system()
    try:
        if sysname == "Windows":
            r = subprocess.run(["schtasks", "/Query", "/TN", _WIN_TASK], capture_output=True,
                               text=True, timeout=10)
            ok = r.returncode == 0
            return _check(ok, f'scheduled task "{_WIN_TASK}" {"registered" if ok else "not registered"}',
                          "powershell -ExecutionPolicy Bypass -File scripts\\install-agent-autostart.ps1")
        if sysname == "Darwin":
            ok = _LAUNCHD_PLIST.exists()
            return _check(ok, f"launchd job {'installed' if ok else 'not installed'}",
                          "bash scripts/install-agent-autostart-mac.sh")
        return _check(True, "autostart not managed on this OS")
    except Exception as e:  # noqa: BLE001
        return _check(False, f"autostart check failed: {e}")


def _git_rev() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                           timeout=5, cwd=str(Path(__file__).resolve().parent.parent))
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def run_checks() -> dict:
    """{'checks': {name: {ok, detail, fix, optional?}}, 'ok': all required pass, 'os', 'version', 'git'}."""
    checks = {
        "python": _python(),
        "crm_token": _crm_token(),
        "playwright": _module("playwright", "pip install -r requirements.txt"),
        "chromium": _playwright_browser(),
        "real_chrome": _real_chrome(),
        "curl_cffi": _module("curl_cffi", "pip install curl_cffi  (TLS-impersonation retry for WAF 403s)",
                             optional=True),
        "patchright": _module("patchright", "pip install patchright  (stealth browser fallback)",
                              optional=True),
        "wa_session": _wa_session(),
        "ai_keys": _ai_keys(),
        "disk": _disk(),
        "autostart": _autostart(),
    }
    required_ok = all(c["ok"] for c in checks.values() if not c.get("optional"))
    return {
        "ok": required_ok,
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "version": AGENT_VERSION,
        "git": _git_rev(),
        "python": platform.python_version(),
        "checks": checks,
    }
