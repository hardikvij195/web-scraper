"""W214 (CRM T1555): remote access to this machine from the owner's PC.

CRM commands (lead_gen_agents.pending_command, arg in command_arg):
  remote_setup <password>  install/configure RustDesk (direct IP over Tailscale, permanent
                           password), Tailscale unattended, no sleep on AC, always-on Claude
  remote_on / remote_off   start / stop RustDesk (the LF agent keeps polling, so on always works)
  claude_on / claude_off   start / stop the always-on `claude remote-control` session

The platform work lives in scripts/remote-access/{mac.sh,windows.ps1}. The password is handed
to the script in the environment (HVT_RD_PASSWORD), never on argv, and is scrubbed from every
line that comes back — nothing that reaches command_result or the logs can contain it.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("webscraper.remote_access")

COMMANDS = ("remote_setup", "remote_on", "remote_off", "claude_on", "claude_off")
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts" / "remote-access"
TIMEOUT_SEC = {"remote_setup": 840}          # installs take a while; under agent.CMD_MAX_SEC (900)
MIN_PASSWORD = 8
RESULT_MAX = 480                              # the Edge Fn keeps 500 chars


def redact(text: str, secret: str) -> str:
    """Every occurrence of the secret becomes ***; empty/short secrets are left alone."""
    return text.replace(secret, "***") if secret and len(secret) >= 4 else text


def _summary(out: str, secret: str) -> str:
    """The script's `RESULT:` lines, else its last lines — redacted, capped to fit the column."""
    lines = [ln.strip() for ln in redact(out, secret).splitlines() if ln.strip()]
    res = [ln[len("RESULT:"):].strip() for ln in lines if ln.startswith("RESULT:")]
    text = "; ".join(res) if res else " | ".join(lines[-4:])
    return text[:RESULT_MAX] or "no output"


def script_cmd(command: str, platform: str = sys.platform) -> list[str]:
    if platform == "win32":
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(SCRIPTS / "windows.ps1"), command]
    if platform == "darwin":
        return ["/bin/bash", str(SCRIPTS / "mac.sh"), command]
    raise RuntimeError(f"remote access is not built for {platform}")


def run(command: str, arg: str | None, device: str = "", platform: str = sys.platform,
        runner=subprocess.run) -> tuple[bool, str]:
    """Run one remote-access command; returns (ok, command_result). Never raises."""
    if command not in COMMANDS:
        return False, f"unknown remote command {command}"
    secret = (arg or "").strip() if command == "remote_setup" else ""
    if command == "remote_setup" and len(secret) < MIN_PASSWORD:
        return False, (f"remote_setup needs the RustDesk permanent password as the command arg "
                       f"(at least {MIN_PASSWORD} characters)")
    try:
        argv = script_cmd(command, platform)
    except RuntimeError as e:
        return False, str(e)
    env = dict(os.environ)
    env["HVT_RD_PASSWORD"] = secret
    env["HVT_DEVICE"] = device or env.get("HVT_AGENT_DEVICE", "")
    log.info("remote access: running %s", command)            # never the arg
    try:
        r = runner(argv, capture_output=True, text=True, env=env,
                   timeout=TIMEOUT_SEC.get(command, 180), encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False, f"{command} timed out on this machine"
    except Exception as e:                                       # noqa: BLE001
        return False, redact(f"{command} could not start: {e}", secret)[:RESULT_MAX]
    summary = _summary((r.stdout or "") + "\n" + (r.stderr or "") if r.returncode else (r.stdout or ""),
                       secret)
    log.info("remote access %s -> rc=%s: %s", command, r.returncode, summary)
    return r.returncode == 0, summary
