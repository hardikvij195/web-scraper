#!/usr/bin/env bash
# fix-agent.sh - one-shot "fix this machine's Lead Finder agent" for macOS / Linux (W182 / CRM T1078,
# 2026-10-09; twin of scripts/fix-agent.ps1). The CRM Systems page shows the one-liner per machine:
#   cd "<root>" && git pull --ff-only; bash scripts/fix-agent.sh
# Add --check to only print identity + evidence and change nothing. bash 3.2 (stock macOS) compatible.
# Exit code 0 = AGENT UP, 1 = not up (the NOT UP block is what to send back).
set -u
CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
LABEL="app.hvtechnologies.leadfinder-agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="data/agent.log"
OS="$(uname -s)"

step() { echo; echo "== $1 =="; }
age_min() {  # minutes since the file was modified, -1 if missing
  if [ ! -e "$1" ]; then echo -1; return; fi
  local m
  if [ "$OS" = "Darwin" ]; then m=$(stat -f %m "$1"); else m=$(stat -c %Y "$1"); fi
  echo $(( ( $(date +%s) - m ) / 60 ))
}
agent_pids() {  # supervisor loop(s) + python agent(s)
  { pgrep -f "run-agent-loop.sh" 2>/dev/null; pgrep -f "webscraper agent" 2>/dev/null; } | grep -v "^$$\$" | sort -u
}
show_procs() {
  local pids; pids="$(agent_pids)"
  if [ -z "$pids" ]; then echo "live processes: none (no supervisor loop, no agent)"; return; fi
  echo "live processes:"
  for p in $pids; do
    ps -o pid= -o etime= -o lstart= -o command= -p "$p" 2>/dev/null | sed 's/^/  /'
  done
}

step "1/4 identity"
REV="$(git rev-parse --short HEAD 2>/dev/null || echo '?')"
VER="$(cat VERSION 2>/dev/null || echo '?')"
DEV="$(cat data/device_name 2>/dev/null || echo '(no data/device_name)')"
echo "machine     : $(hostname) ($OS, user $(id -un))"
echo "root        : $ROOT"
echo "git rev     : $REV   VERSION $VER"
echo "device_name : $DEV"
echo "time        : $(date '+%Y-%m-%d %H:%M:%S')"

step "2/4 evidence"
if [ -f "$LOG" ]; then
  echo "--- last 40 lines of data/agent.log (modified $(age_min "$LOG") min ago) ---"
  tail -n 40 "$LOG"
  echo "--- end of log ---"
else
  echo "no agent.log"
fi
if [ -f data/agent.stop ]; then echo "agent.stop  : PRESENT (a CRM Stop was pressed - the loop refuses to start while it exists)"; else echo "agent.stop  : absent"; fi
A=$(age_min data/agent.alive)
if [ "$A" -lt 0 ]; then echo "agent.alive : missing (agent older than W182, or never polled the CRM)"; else echo "agent.alive : touched $A min ago (healthy = under 1 min)"; fi
LAUNCHD_LOADED=0
if [ "$OS" = "Darwin" ]; then
  if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    LAUNCHD_LOADED=1
    echo "launchd job $LABEL: loaded"
    launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E "state =|pid =|last exit code" | sed 's/^/  /'
  elif [ -f "$PLIST" ]; then
    echo "launchd job $LABEL: plist present but NOT LOADED"
  else
    echo "launchd job $LABEL: NOT REGISTERED (no $PLIST)"
  fi
fi
show_procs

if [ "$CHECK" = "1" ]; then
  echo
  echo "(--check: nothing changed)"
  exit 0
fi

step "3/4 fix"
if [ -f data/agent.stop ]; then rm -f data/agent.stop; echo "removed data/agent.stop"; fi
# On macOS a loaded launchd job (KeepAlive) is restarted by launchd itself below; kill the python
# agents here so a stale one never survives under a fresh loop. Orphan loops die with their agent.
for p in $(agent_pids); do
  if kill -9 "$p" 2>/dev/null; then echo "killed PID $p"; fi
done
echo "git pull --ff-only (60 s stall timeout) ..."
GIT_TERMINAL_PROMPT=0 git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 pull --ff-only 2>&1 | sed 's/^/  /'
PY=".venv/bin/python"; [ -x "$PY" ] || PY="python3"
echo "pip install -r requirements.txt (timeout 30 s, 2 retries) ..."
"$PY" -m pip install -q --timeout 30 --retries 2 -r requirements.txt 2>&1 | sed 's/^/  /'
OFFSET=0
[ -f "$LOG" ] && OFFSET=$(wc -c < "$LOG" | tr -d ' ')
if [ "$OS" = "Darwin" ]; then
  if [ ! -f "$PLIST" ]; then
    echo "registering the launchd job ..."
    bash scripts/install-agent-autostart-mac.sh
  elif launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null; then
    echo "launchctl kickstart -k $LABEL: ok"
  else
    echo "kickstart failed - bootstrapping $PLIST ..."
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>&1 | sed 's/^/  /'
    launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null || true
  fi
else
  echo "starting ./run-agent-loop.sh (nohup) ..."
  chmod +x run-agent-loop.sh 2>/dev/null
  nohup ./run-agent-loop.sh >/dev/null 2>&1 &
fi

step "4/4 verdict"
UP=""
i=0
while [ $i -lt 18 ]; do
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
  echo "AGENT UP"
  echo "$UP"
  echo "The CRM card for this machine turns online within ~1 minute."
  exit 0
fi
echo "NOT UP - no 'agent up (' line within 90 s. Send everything between the lines below:"
echo "-------- NOT UP: $(hostname)  root=$ROOT  rev=$REV  VERSION=$VER --------"
if [ -f "$LOG" ]; then tail -n 15 "$LOG"; else echo "no agent.log"; fi
echo "processes now: $(agent_pids | tr '\n' ' ')"
echo "hint: run  $PY -m webscraper doctor  in this folder and send its output too."
echo "-------- end --------"
echo "The CRM card for this machine turns online within ~1 minute."
exit 1
