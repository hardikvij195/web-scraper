#!/usr/bin/env bash
# One-click installer for the HVT Lead Finder agent on macOS (Linux works too).
# Downloaded from the CRM (Lead Finder → Setup → "Install agent on this computer"),
# which embeds the token + device name; also runnable by hand:
#   bash install-agent.sh --token wsk_… [--device "Hardik-MacBook"] [--dir ~/hvt-lead-finder-agent] [--repo URL]
# Idempotent: re-running updates the checkout, deps and .env, then restarts the agent.
set -euo pipefail

# DIR empty = auto-detect: an earlier install (launchd plist's WorkingDirectory), else a
# checkout holding webscraper/agent.py at/above the current folder, else ~/hvt-lead-finder-agent.
TOKEN=""; DEVICE=""; DIR=""; CRM_URL=""
REPO="${LEAD_FINDER_AGENT_REPO:-https://github.com/hardikvij195/web-scraper.git}"
while [ $# -gt 0 ]; do
  case "$1" in
    --token) TOKEN="$2"; shift 2;;
    --device) DEVICE="$2"; shift 2;;
    --dir) DIR="$2"; shift 2;;
    --repo) REPO="$2"; shift 2;;
    # The CRM's Supabase URL. A CLONED tenant CRM lives on its own project; without this
    # the agent would talk to HVT's (the built-in default) and never see the tenant's jobs.
    --crm-url) CRM_URL="$2"; shift 2;;
    --skip-wa-login) SKIP_WA_LOGIN=1; shift;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
[ -n "$TOKEN" ] || { echo "need --token wsk_… (CRM → Lead Finder → Setup → Agent tokens)" >&2; exit 2; }
DEVICE="${DEVICE:-$(scutil --get ComputerName 2>/dev/null || hostname)}"
is_checkout() { [ -n "$1" ] && [ -f "$1/webscraper/agent.py" ]; }
if [ -z "$DIR" ]; then
  PLIST="$HOME/Library/LaunchAgents/app.hvtechnologies.leadfinder-agent.plist"
  if [ -f "$PLIST" ]; then
    WD=$(sed -n 's|.*<key>WorkingDirectory</key><string>\(.*\)</string>.*|\1|p' "$PLIST" | head -1)
    if is_checkout "$WD"; then DIR="$WD"; echo "using the existing install: $DIR"; fi
  fi
fi
if [ -z "$DIR" ]; then
  D="$PWD"
  while [ -n "$D" ] && [ "$D" != "/" ]; do
    if is_checkout "$D"; then DIR="$D"; echo "using the checkout found at: $DIR"; break; fi
    D=$(dirname "$D")
  done
fi
[ -n "$DIR" ] || DIR="$HOME/hvt-lead-finder-agent"

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

step "1/7 tools (git, python3.11+)"
if ! command -v git >/dev/null 2>&1; then
  if [ "$(uname)" = "Darwin" ]; then
    echo "git missing — installing Xcode Command Line Tools (a dialog opens; re-run this installer after it finishes)"
    xcode-select --install || true; exit 3
  fi
  echo "git missing — install it and re-run" >&2; exit 3
fi
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  if command -v brew >/dev/null 2>&1; then
    echo "python 3.11+ missing — brew install python@3.13"; brew install python@3.13; PY="$(brew --prefix python@3.13)/bin/python3.13"
  else
    echo "python 3.11+ missing — install from https://www.python.org/downloads/macos/ (or Homebrew) and re-run" >&2; exit 3
  fi
fi
echo "using $($PY --version) at $(command -v "$PY")"

step "2/7 code → $DIR"
# An existing checkout is PULLED, never re-cloned. `.git` is a FILE inside a mono-repo
# submodule (hv-technologies/web-scraper), so test with git itself, not `-d .git`
# (2026-08-26: the Mac hit "destination path already exists and is not an empty directory").
if git -C "$DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  # A submodule checkout is on a detached HEAD, where `git pull` refuses to run (and so
  # would the self-updating loop). Put it on main tracking origin/main once.
  if ! git -C "$DIR" symbolic-ref -q HEAD >/dev/null; then
    git -C "$DIR" fetch -q origin main && git -C "$DIR" checkout -q -B main origin/main || true
  fi
  git -C "$DIR" pull --ff-only || echo "pull failed (local changes?) — continuing with the current checkout"
elif [ -d "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
  if [ -f "$DIR/webscraper/agent.py" ]; then
    echo "$DIR holds the scraper but is not a git checkout — using it as is (no updates via git)"
  else
    echo "ERROR: $DIR exists, is not empty, and is not the web-scraper repo. Pick another --dir or clone the mono repo with: git submodule update --init"; exit 1
  fi
else
  git clone --depth 1 "$REPO" "$DIR"
fi
cd "$DIR"

step "3/7 python deps"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt
.venv/bin/python -m playwright install chromium

step "4/7 .env"
touch .env
# replace-or-append each key; keep everything else the user put there
for kv in "CRM_AGENT_TOKEN=$TOKEN" "LEAD_FINDER_DEVICE=$DEVICE" ${CRM_URL:+"VITE_SUPABASE_URL=$CRM_URL"}; do
  k="${kv%%=*}"
  if grep -q "^$k=" .env; then
    tmp="$(mktemp)"; grep -v "^$k=" .env > "$tmp"; mv "$tmp" .env
  fi
  echo "$kv" >> .env
done
chmod 600 .env

step "5/7 autostart + start"
# Re-running the installer = "update + restart" for a machine whose agent predates remote
# commands (2026-08-26): stop whatever is running the old code first.
pkill -f "run-agent-loop.sh" 2>/dev/null || true
pkill -f "webscraper agent" 2>/dev/null || true
if [ "$(uname)" = "Darwin" ]; then
  bash scripts/install-agent-autostart-mac.sh
else
  chmod +x run-agent-loop.sh; (nohup ./run-agent-loop.sh >/dev/null 2>&1 &)
  echo "started run-agent-loop.sh in the background (no launchd/systemd on this OS — add one if you need boot start)"
fi

step "6/7 self-check"
.venv/bin/python -m webscraper doctor || true
echo
# Prove the agent is alive before saying "Done" (2026-08-27): wait for the loop to start
# it, then show the log tail — "agent up" means the CRM sees this machine within ~10 s.
echo "waiting for the agent to start..."
ALIVE=0
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  sleep 5
  if pgrep -f "webscraper agent" >/dev/null 2>&1; then ALIVE=1; break; fi
done
LOG="$DIR/data/agent.log"
if [ "$ALIVE" = "1" ] && [ -f "$LOG" ] && tail -n 12 "$LOG" | grep -q "agent up"; then
  echo "agent is running and talking to the CRM."
elif [ "$ALIVE" = "1" ]; then
  echo "agent process is running; last log lines (look for errors):"; [ -f "$LOG" ] && tail -n 12 "$LOG" | sed 's/^/  /'
else
  echo "agent did NOT start. Last log lines:"; [ -f "$LOG" ] && tail -n 12 "$LOG" | sed 's/^/  /'
  echo "Try: cd '$DIR' && ./run-agent.sh to see the error, or send $LOG"
fi
echo "Done. This machine is '$DEVICE' in the CRM's Run-on list within ~10 s. Log: $LOG"
# 7/7 - WhatsApp link, inline, so the one-time QR scan happens before the terminal closes.
LINKED=$(.venv/bin/python -c "from webscraper.store import Store; from webscraper.wa_verify import profile_dir; print(int(any(not a['disabled'] and (profile_dir(a['name'])/'Default').exists() for a in Store().list_wa_accounts())))" 2>/dev/null || echo 0)
if [ "${SKIP_WA_LOGIN:-0}" = "1" ]; then
  echo "WhatsApp link skipped (--skip-wa-login). Later: cd '$DIR' && .venv/bin/python -m webscraper wa-login main"
elif [ "$LINKED" = "1" ]; then
  echo "WhatsApp already linked on this machine - skipping the QR step."
else
  step "7/7 WhatsApp link (one-time QR scan)"
  echo "A WhatsApp Web window opens now. On the phone: WhatsApp > Linked devices > Link a device > scan the QR (2 min)."
  .venv/bin/python -m webscraper wa-login main || echo "wa-login did not finish - re-run any time: cd '$DIR' && .venv/bin/python -m webscraper wa-login main"
fi
