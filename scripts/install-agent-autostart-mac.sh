#!/usr/bin/env bash
# Installs a launchd user agent so the CRM Lead Finder agent starts at login on this Mac
# and is restarted if it dies (KeepAlive). macOS twin of install-agent-autostart.ps1.
#   Install:   bash scripts/install-agent-autostart-mac.sh
#   Uninstall: launchctl bootout gui/$(id -u)/app.hvtechnologies.leadfinder-agent; rm ~/Library/LaunchAgents/app.hvtechnologies.leadfinder-agent.plist
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="app.hvtechnologies.leadfinder-agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
chmod +x "$ROOT/run-agent-loop.sh"
mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/data"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$ROOT/run-agent-loop.sh</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$ROOT/data/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$ROOT/data/launchd.err.log</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed + started $LABEL. Log: $ROOT/data/agent.log"
