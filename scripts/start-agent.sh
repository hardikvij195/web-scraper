#!/usr/bin/env bash
# start-agent.sh - START this machine's Lead Finder agent back, macOS / Linux (CRM T1102, 2026-10-11;
# twin of scripts/start-agent.ps1). The CRM Systems card shows the one-liner per machine:
#   cd "<root>" && git pull --ff-only; bash scripts/start-agent.sh
# 1) removes data/agent.stop  2) POST lead-finder-agent action self_start (machine switched ON in the CRM,
# so the restarted agent is offered jobs instead of sitting online + parked)  3) macOS: launchd kickstart -k,
# bootstrap first when the job is not loaded (same rule as fix-agent.sh, T566); Linux: nohup the loop
# 4) waits up to 60 s for a fresh "agent up (" line in data/agent.log -> STARTED, else NOT UP + log tail.
# --dry-run prints what it would do and changes nothing. bash 3.2 (stock macOS) compatible.
set -u
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
LABEL="app.hvtechnologies.leadfinder-agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="data/agent.log"
OS="$(uname -s)"

envval() {  # process env wins, then .env (as config.py load_dotenv)
  local v="${!1:-}"
  if [ -z "$v" ] && [ -f .env ]; then
    v="$(grep -E "^[[:space:]]*(export[[:space:]]+)?$1[[:space:]]*=" .env | tail -n 1 | sed -E "s/^[^=]*=[[:space:]]*//; s/[[:space:]]*\$//; s/^\"(.*)\"\$/\1/; s/^'(.*)'\$/\1/")"
  fi
  printf '%s' "$v"
}
TOKEN="$(envval CRM_AGENT_TOKEN)"; [ -n "$TOKEN" ] || TOKEN="$(envval AGENT_TOKEN)"
BASE="$(envval CRM_FUNCTIONS_URL)"
if [ -z "$BASE" ]; then
  SUPA="$(envval VITE_SUPABASE_URL)"
  if [ -n "$SUPA" ]; then BASE="${SUPA%/}/functions/v1"; else BASE="https://fyfhkjxewzbyxdwspkuc.supabase.co/functions/v1"; fi
fi
URL="${BASE%/}/lead-finder-agent"
DEV="$(cat data/device_name 2>/dev/null | tr -d '\r\n')"
[ -n "$DEV" ] || DEV="$(envval LEAD_FINDER_DEVICE)"
[ -n "$DEV" ] || DEV="$(hostname)"
json_str() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

echo "start-agent: $ROOT  (machine $(hostname), $OS)"
echo "CRM: $URL  device '$DEV'  token $([ -n "$TOKEN" ] && echo found || echo 'MISSING (.env CRM_AGENT_TOKEN)')"
if [ "$DRY" = "1" ]; then
  echo "(--dry-run) data/agent.stop: $([ -f data/agent.stop ] && echo 'present - would remove' || echo absent)"
  echo "(--dry-run) would POST action self_start for '$DEV'"
  if [ "$OS" = "Darwin" ]; then echo "(--dry-run) would kickstart (or bootstrap $PLIST) gui/$(id -u)/$LABEL"; else echo "(--dry-run) would nohup ./run-agent-loop.sh"; fi
  exit 0
fi

if [ -f data/agent.stop ]; then rm -f data/agent.stop; echo "removed data/agent.stop"; else echo "data/agent.stop: absent"; fi

if [ -z "$TOKEN" ]; then
  echo "WARNING: no CRM_AGENT_TOKEN in .env - the CRM was NOT told. If the card says Stopped, press Start agent on it."
else
  RESP="$(curl -sS -m 30 -X POST "$URL" -H "X-Agent-Token: $TOKEN" -H "Content-Type: application/json" \
    -d "{\"action\":\"self_start\",\"device\":\"$(json_str "$DEV")\"}" -w '\n%{http_code}' 2>&1)"
  CODE="$(printf '%s' "$RESP" | tail -n 1)"
  if [ "$CODE" = "200" ]; then echo "CRM told: '$DEV' switched on - it takes jobs again."
  else echo "WARNING: could not tell the CRM (HTTP $CODE: $(printf '%s' "$RESP" | head -n 1)). If the card says Stopped, press Start agent on it."; fi
fi

OFFSET=0
[ -f "$LOG" ] && OFFSET=$(wc -c < "$LOG" | tr -d ' ')
if [ "$OS" = "Darwin" ]; then
  if [ ! -f "$PLIST" ]; then
    echo "registering the launchd job ..."
    bash scripts/install-agent-autostart-mac.sh
  elif launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>&1 | sed 's/^/  /'
    echo "launchctl kickstart -k $LABEL"
  else
    echo "bootstrapping $PLIST ..."
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>&1 | sed 's/^/  /'
    launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null || true
  fi
else
  if pgrep -f "run-agent-loop.sh" >/dev/null 2>&1; then
    echo "run-agent-loop.sh already running"
  else
    echo "starting ./run-agent-loop.sh (nohup) ..."
    chmod +x run-agent-loop.sh 2>/dev/null
    nohup ./run-agent-loop.sh >/dev/null 2>&1 &
  fi
fi

UP=""
i=0
while [ $i -lt 12 ]; do
  sleep 5
  if [ -f "$LOG" ]; then
    UP="$(tail -c +$((OFFSET + 1)) "$LOG" 2>/dev/null | grep "agent up (" | tail -n 1)"
    [ -n "$UP" ] && break
  fi
  printf "."
  i=$((i + 1))
done
echo
if [ -n "$UP" ]; then
  echo "STARTED"
  echo "$UP"
  echo "Its jobs resume by themselves (in-flight ones first). The CRM card turns online within ~1 minute."
  exit 0
fi
echo "NOT UP - no 'agent up (' line within 60 s. Last 15 lines of data/agent.log:"
if [ -f "$LOG" ]; then tail -n 15 "$LOG"; else echo "no agent.log"; fi
echo "If it stays down, paste the Fix command from the CRM Systems card (pull + reinstall + restart)."
exit 1
