#!/usr/bin/env bash
# stop-agent.sh - deliberately STOP this machine's Lead Finder agent, macOS / Linux (CRM T1102, 2026-10-11;
# twin of scripts/stop-agent.ps1). The CRM Systems card shows the one-liner per machine:
#   cd "<root>" && git pull --ff-only; bash scripts/stop-agent.sh
# 1) writes data/agent.stop  2) macOS: launchctl bootout of the launchd job (KeepAlive would restart it)
# 3) TERM the loop + python agent, wait up to 20 s (--hard: no wait), then kill -9 what is left, plus
#    Chrome / node processes whose command line contains THIS folder (never the user's own Chrome)
# 4) POST lead-finder-agent action self_stop: machine switched off in the CRM, its claimed / running jobs
#    back to the queue unpinned WITHOUT a T1101 loss. A failed POST only prints a warning.
# --dry-run prints what it would do and changes nothing. bash 3.2 (stock macOS) compatible.
set -u
HARD=0; DRY=0
for a in "$@"; do
  case "$a" in --hard|-Hard) HARD=1 ;; --dry-run|-DryRun) DRY=1 ;; esac
done
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
LABEL="app.hvtechnologies.leadfinder-agent"
OS="$(uname -s)"

# --- CRM target: same sources as `python -m webscraper agent --crm` (cli.py + config.py load_dotenv(ROOT/.env);
# the process env wins). Device = data/device_name (agent.py _device_name) > LEAD_FINDER_DEVICE > hostname.
envval() {
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

pid_cwd() {  # working directory of a process (macOS lsof, Linux /proc)
  if [ -e "/proc/$1/cwd" ]; then readlink "/proc/$1/cwd" 2>/dev/null; else lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1; fi
}
agent_pids() {  # loop + python agent + Chrome/node of THIS root only (another checkout is left alone)
  {
    for p in $(pgrep -f "run-agent-loop.sh" 2>/dev/null; pgrep -f "webscraper agent" 2>/dev/null); do
      if ps -o command= -p "$p" 2>/dev/null | grep -qF "$ROOT" || [ "$(pid_cwd "$p")" = "$ROOT" ]; then echo "$p"; fi
    done
    ps -axo pid=,command= 2>/dev/null | grep -F "$ROOT" | grep -iE "chrom|node|headless_shell" | grep -v grep | awk '{print $1}'
  } | grep -v "^$$\$" | sort -u
}

echo "stop-agent: $ROOT  (machine $(hostname), $OS)"
echo "CRM: $URL  device '$DEV'  token $([ -n "$TOKEN" ] && echo found || echo 'MISSING (.env CRM_AGENT_TOKEN)')"
PIDS="$(agent_pids)"
if [ "$DRY" = "1" ]; then
  echo "(--dry-run) would write data/agent.stop"
  [ "$OS" = "Darwin" ] && echo "(--dry-run) would launchctl bootout gui/$(id -u)/$LABEL"
  if [ -z "$PIDS" ]; then echo "(--dry-run) no agent processes running from here"; fi
  for p in $PIDS; do echo "(--dry-run) would stop $(ps -o pid= -o command= -p "$p" 2>/dev/null)"; done
  echo "(--dry-run) would POST action self_stop for '$DEV'"
  exit 0
fi

mkdir -p data
echo "stopped by hand (scripts/stop-agent.sh) $(date '+%Y-%m-%d %H:%M:%S')" > data/agent.stop
echo "wrote data/agent.stop"
if [ "$OS" = "Darwin" ] && launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  # bootout sends SIGTERM to the job and keeps launchd's KeepAlive from relaunching it
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "launchd job $LABEL booted out"
fi
KILLED=""
if [ -n "$PIDS" ] && [ "$HARD" = "0" ]; then
  for p in $PIDS; do kill -TERM "$p" 2>/dev/null; done
  printf "waiting up to 20 s for a graceful exit "
  i=0
  while [ $i -lt 10 ]; do
    sleep 2
    [ -z "$(agent_pids)" ] && break
    printf "."
    i=$((i + 1))
  done
  echo
  for p in $PIDS; do kill -0 "$p" 2>/dev/null || KILLED="$KILLED $p(exited)"; done
fi
for p in $(agent_pids); do
  if kill -9 "$p" 2>/dev/null; then KILLED="$KILLED $p"; fi
done

CRM=""
if [ -z "$TOKEN" ]; then
  CRM="WARNING: no CRM_AGENT_TOKEN in .env - the CRM was NOT told. Press Stop agent on the Systems card, or its jobs wait for auto-heal."
else
  RESP="$(curl -sS -m 30 -X POST "$URL" -H "X-Agent-Token: $TOKEN" -H "Content-Type: application/json" \
    -d "{\"action\":\"self_stop\",\"device\":\"$(json_str "$DEV")\"}" -w '\n%{http_code}' 2>&1)"
  CODE="$(printf '%s' "$RESP" | tail -n 1)"
  if [ "$CODE" = "200" ]; then
    N="$(printf '%s' "$RESP" | sed -n 's/.*"requeued":\[\([^]]*\)\].*/\1/p' | tr ',' '\n' | grep -c '[0-9]')"
    CRM="CRM told: '$DEV' switched off; $N running job(s) put back in the queue (no T1101 loss)."
  else
    CRM="WARNING: could not tell the CRM (HTTP $CODE: $(printf '%s' "$RESP" | head -n 1)). The agent is stopped anyway; press Stop agent on the Systems card so its jobs move now."
  fi
fi

echo
echo "STOPPED"
if [ -z "$KILLED" ]; then echo "killed: nothing (no agent processes were left running)"; else echo "killed:$KILLED"; fi
echo "$CRM"
echo "Start it back with:  bash scripts/start-agent.sh   (or the Start command on the CRM Systems card)"
exit 0
