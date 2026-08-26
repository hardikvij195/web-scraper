#!/usr/bin/env bash
# Supervisor: keeps the CRM Lead Finder agent alive forever (macOS / Linux twin of
# run-agent-loop.bat). If the agent crashes (network drop, Playwright hiccup) it
# restarts after 15s. Logs to data/agent.log. Started by the launchd job installed
# with scripts/install-agent-autostart-mac.sh; also safe to run by hand.
set -u
cd "$(dirname "$0")"
PY=".venv/bin/python"; [ -x "$PY" ] || PY="python3"
mkdir -p data
# Friendly machine name shown in the CRM "Run on" picker; override in .env.
export LEAD_FINDER_DEVICE="${LEAD_FINDER_DEVICE:-$(scutil --get ComputerName 2>/dev/null || hostname)}"
while true; do
  # Self-update before every start (2026-08-26): a CRM feature that needs new agent code
  # (the Start-WhatsApp-session button) sat at "waiting for <Mac> to pick it up" because
  # the Mac still ran the commit it was installed from. Fast-forward only, never blocks:
  # offline or a diverged tree just runs whatever is on disk. `agent exited` → restart
  # is therefore also "upgrade" — the CRM can bounce an agent to update it.
  if git pull --ff-only -q 2>>data/agent.log; then
    "$PY" -m pip install -q -r requirements.txt >>data/agent.log 2>&1 || true
  fi
  echo "[$(date '+%d-%m-%Y %H.%M.%S')] starting agent ($(git rev-parse --short HEAD 2>/dev/null))" >> data/agent.log
  "$PY" -m webscraper agent --crm --poll 5 >> data/agent.log 2>&1
  echo "[$(date '+%d-%m-%Y %H.%M.%S')] agent exited (code $?) - restarting in 15s" >> data/agent.log
  sleep 15
done
