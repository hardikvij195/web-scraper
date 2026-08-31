#!/usr/bin/env bash
# Supervisor: keeps the CRM Lead Finder agent alive forever (macOS / Linux twin of
# run-agent-loop.bat). If the agent crashes (network drop, Playwright hiccup) it
# restarts after 15s. Logs to data/agent.log. Started by the launchd job installed
# with scripts/install-agent-autostart-mac.sh; also safe to run by hand.
set -u
cd "$(dirname "$0")"
PY=".venv/bin/python"; [ -x "$PY" ] || PY="python3"
mkdir -p data
# Friendly machine name shown in the CRM "Run on" picker. `.env` (written by the
# installer's --device) wins; otherwise the Mac's ComputerName. Never export the bare
# hostname: under launchd `scutil` can return nothing and the agent then registered as
# "Unknown_26:e5:…" — a NEW device, so the job pinned to the old name and the queued
# wa-login command were orphaned (2026-08-26).
if ! grep -q '^LEAD_FINDER_DEVICE=' .env 2>/dev/null && [ -z "${LEAD_FINDER_DEVICE:-}" ]; then
  _name="$(scutil --get ComputerName 2>/dev/null || true)"
  [ -n "$_name" ] && export LEAD_FINDER_DEVICE="$_name"
fi
# Starting by hand (or via launchd) clears a previous CRM "Stop agent" — otherwise
# the sentinel would keep the machine stopped for ever with no local way back, since
# a stopped agent polls nothing and cannot be told to start.
if [ -f data/agent.stop ]; then
  echo "[$(date '+%d-%m-%Y %H.%M.%S')] clearing stop sentinel - starting again" >> data/agent.log
  rm -f data/agent.stop
fi

while true; do
  # W50: the CRM's "Stop agent" drops data/agent.stop. Without this check the loop
  # restarted the agent 15s later and the folder stayed locked, which is why an old
  # folder could not be deleted after a move.
  if [ -f data/agent.stop ]; then
    echo "[$(date '+%d-%m-%Y %H.%M.%S')] stop sentinel present - supervisor exiting" >> data/agent.log
    exit 0
  fi
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
