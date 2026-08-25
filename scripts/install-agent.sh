#!/usr/bin/env bash
# One-click installer for the HVT Lead Finder agent on macOS (Linux works too).
# Downloaded from the CRM (Lead Finder → Setup → "Install agent on this computer"),
# which embeds the token + device name; also runnable by hand:
#   bash install-agent.sh --token wsk_… [--device "Hardik-MacBook"] [--dir ~/hvt-lead-finder-agent] [--repo URL]
# Idempotent: re-running updates the checkout, deps and .env, then restarts the agent.
set -euo pipefail

TOKEN=""; DEVICE=""; DIR="$HOME/hvt-lead-finder-agent"; CRM_URL=""
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
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
[ -n "$TOKEN" ] || { echo "need --token wsk_… (CRM → Lead Finder → Setup → Agent tokens)" >&2; exit 2; }
DEVICE="${DEVICE:-$(scutil --get ComputerName 2>/dev/null || hostname)}"

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

step "1/6 tools (git, python3.11+)"
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

step "2/6 code → $DIR"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only || echo "pull failed (local changes?) — continuing with the current checkout"
else
  git clone --depth 1 "$REPO" "$DIR"
fi
cd "$DIR"

step "3/6 python deps"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt
.venv/bin/python -m playwright install chromium

step "4/6 .env"
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

step "5/6 autostart + start"
if [ "$(uname)" = "Darwin" ]; then
  bash scripts/install-agent-autostart-mac.sh
else
  chmod +x run-agent-loop.sh; (nohup ./run-agent-loop.sh >/dev/null 2>&1 &)
  echo "started run-agent-loop.sh in the background (no launchd/systemd on this OS — add one if you need boot start)"
fi

step "6/6 self-check"
.venv/bin/python -m webscraper doctor || true
echo
echo "Done. This machine is '$DEVICE' in the CRM's Run-on list within ~10 s. Log: $DIR/data/agent.log"
echo "WhatsApp verification also needs: cd '$DIR' && .venv/bin/python -m webscraper wa-login main"
