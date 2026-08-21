#!/usr/bin/env bash
# Start the Lead Finder scraper agent on Linux/macOS/VPS. Reads CRM_AGENT_TOKEN from .env.
# One-time setup: python -m venv .venv && . .venv/bin/activate &&
#   pip install -r requirements.txt && playwright install --with-deps chromium
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x ".venv/bin/python" ]; then
  echo "Not set up — create .venv and pip install -r requirements.txt first." >&2
  exit 1
fi
exec ".venv/bin/python" -m webscraper agent --crm --poll 15
