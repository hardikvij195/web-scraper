#!/bin/bash
# W214 (CRM T1555): remote access on a Lead Finder Mac. Called by webscraper/remote_access.py:
#   mac.sh remote_setup|remote_on|remote_off|claude_on|claude_off
# The RustDesk password arrives in $HVT_RD_PASSWORD only (never argv, never echoed).
# Every line starting "RESULT:" becomes part of the CRM command_result.
set -u
CMD="${1:-}"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.claude/local:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"                  # web-scraper checkout
WORK="$(cd "$ROOT/.." && pwd)"                     # its parent (hv-technologies folder)
DEVICE="${HVT_DEVICE:-$(hostname -s)}"
RD_APP="/Applications/RustDesk.app"
RD_BIN="$RD_APP/Contents/MacOS/RustDesk"
RC_LABEL="app.hvtechnologies.claude-rc"
RC_PLIST="$HOME/Library/LaunchAgents/$RC_LABEL.plist"
RC_LOG="$HOME/Library/Logs/hvt-claude-rc.log"
UIDN="$(id -u)"
FAIL=0
res() { echo "RESULT: $*"; }
owner() { echo "RESULT: OWNER: $*"; }
can_sudo() { sudo -n true 2>/dev/null; }

install_rustdesk() {
  [ -x "$RD_BIN" ] && { res "RustDesk already installed"; return 0; }
  if command -v brew >/dev/null 2>&1 && brew install --cask rustdesk >/dev/null 2>&1 && [ -x "$RD_BIN" ]; then
    res "RustDesk installed (brew cask)"; return 0
  fi
  local arch url tmp mnt
  arch="$(uname -m)"; [ "$arch" = "arm64" ] && arch="aarch64"
  url="$(curl -fsSL https://api.github.com/repos/rustdesk/rustdesk/releases/latest \
        | grep -o "https://[^\"]*-${arch}\.dmg" | head -1)"
  [ -z "$url" ] && { res "RustDesk install FAILED (no .dmg for $arch found)"; FAIL=1; return 1; }
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/rd.dmg" "$url" || { res "RustDesk download FAILED"; FAIL=1; return 1; }
  mnt="$(hdiutil attach -nobrowse -quiet "$tmp/rd.dmg" | awk -F'\t' '/\/Volumes\//{print $NF}' | tail -1)"
  if [ -n "$mnt" ] && [ -d "$mnt/RustDesk.app" ]; then
    if cp -R "$mnt/RustDesk.app" /Applications/ 2>/dev/null || { can_sudo && sudo -n cp -R "$mnt/RustDesk.app" /Applications/; }; then
      res "RustDesk installed (official .dmg)"
    else
      res "RustDesk copy to /Applications FAILED (permission)"; FAIL=1
    fi
    hdiutil detach -quiet "$mnt" || true
  else
    res "RustDesk .dmg could not be mounted"; FAIL=1
  fi
  rm -rf "$tmp"
  [ -x "$RD_BIN" ]
}

# key = value in the [options] table of RustDesk2.toml (used when the CLI --option is refused)
toml_set() {
  local f="$1" k="$2" v="$3"
  mkdir -p "$(dirname "$f")"; [ -f "$f" ] || printf "rendezvous_server = ''\nnat_type = 1\nserial = 0\n\n[options]\n" > "$f"
  grep -q '^\[options\]' "$f" || printf "\n[options]\n" >> "$f"
  if grep -q "^$k = " "$f"; then
    sed -i '' "s|^$k = .*|$k = '$v'|" "$f"
  else
    awk -v line="$k = '$v'" '{print} /^\[options\]/{print line}' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  fi
}

configure_rustdesk() {
  local cfg="$HOME/Library/Preferences/com.carriez.RustDesk/RustDesk2.toml"
  pkill -x RustDesk 2>/dev/null; sleep 1
  for kv in "direct-server Y" "direct-access-port 21118" "approve-mode password" \
            "verification-method use-permanent-password" "allow-auto-disconnect N"; do
    set -- $kv
    toml_set "$cfg" "$1" "$2"
    can_sudo && sudo -n "$RD_BIN" --option "$1" "$2" >/dev/null 2>&1 || true
  done
  res "RustDesk options set: direct IP access on 21118, password-only approve"
  if [ -n "${HVT_RD_PASSWORD:-}" ]; then
    if can_sudo && sudo -n "$RD_BIN" --password "$HVT_RD_PASSWORD" >/dev/null 2>&1; then
      res "RustDesk permanent password set"
    elif "$RD_BIN" --password "$HVT_RD_PASSWORD" >/dev/null 2>&1; then
      res "RustDesk permanent password set (user)"
    else
      res "RustDesk permanent password NOT set"; owner "set it in RustDesk > Settings > Security"; FAIL=1
    fi
  fi
  rustdesk_on
}

rustdesk_on() {
  [ -x "$RD_BIN" ] || { res "RustDesk not installed - send remote_setup first"; FAIL=1; return; }
  rm -f "$ROOT/data/remote.off"
  for p in /Library/LaunchDaemons/com.carriez.RustDesk_service.plist; do
    [ -f "$p" ] && can_sudo && sudo -n launchctl bootstrap system "$p" 2>/dev/null
  done
  for p in /Library/LaunchAgents/com.carriez.RustDesk_server.plist; do
    [ -f "$p" ] && launchctl bootstrap "gui/$UIDN" "$p" 2>/dev/null
  done
  pgrep -x RustDesk >/dev/null || open -g -a "$RD_APP" 2>/dev/null
  sleep 3
  if pgrep -x RustDesk >/dev/null; then res "RustDesk running - connect to $(tailscale_ip)"; else res "RustDesk did NOT start"; FAIL=1; fi
}

rustdesk_off() {
  mkdir -p "$ROOT/data"; date > "$ROOT/data/remote.off"
  launchctl bootout "gui/$UIDN/com.carriez.RustDesk_server" 2>/dev/null
  can_sudo && sudo -n launchctl bootout system/com.carriez.RustDesk_service 2>/dev/null
  pkill -x RustDesk 2>/dev/null; can_sudo && sudo -n pkill -x RustDesk 2>/dev/null; sleep 2
  if pgrep -x RustDesk >/dev/null; then
    res "RustDesk still running (its root service needs sudo)"; owner "quit RustDesk on the Mac or allow passwordless sudo"; FAIL=1
  else
    res "RustDesk stopped - screens disabled (LF agent still polling; remote_on brings it back)"
  fi
}

tailscale_ip() {
  local ts; ts="$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)"
  "$ts" ip -4 2>/dev/null | head -1 || echo "?"
}

tailscale_check() {
  local ts; ts="$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)"
  if [ -x "$ts" ] || command -v tailscale >/dev/null; then
    res "Tailscale $("$ts" ip -4 2>/dev/null | head -1)"
    if pgrep -f tailscaled >/dev/null && ! pgrep -f "Tailscale.app" >/dev/null; then
      res "Tailscale runs as a system daemon (starts at boot)"
    else
      owner "Tailscale app: enable 'Launch at login' (macOS app connects after login only)"
    fi
  else
    res "Tailscale not found"; owner "install + log in to Tailscale"; FAIL=1
  fi
}

no_sleep() {
  if can_sudo && sudo -n pmset -c sleep 0 disksleep 0 womp 1 autorestart 1 >/dev/null 2>&1; then
    res "system sleep on AC disabled (pmset -c sleep 0)"
  else
    res "no passwordless sudo - sleep NOT changed"; owner "run once: sudo pmset -c sleep 0 disksleep 0 womp 1 autorestart 1"
  fi
}

claude_bin() { command -v claude || { [ -x "$HOME/.local/bin/claude" ] && echo "$HOME/.local/bin/claude"; }; }

install_claude() {
  [ -n "$(claude_bin)" ] && { res "Claude Code present ($("$(claude_bin)" --version 2>/dev/null | head -1))"; return 0; }
  curl -fsSL https://claude.ai/install.sh | bash >/dev/null 2>&1 || { command -v npm >/dev/null && npm i -g @anthropic-ai/claude-code >/dev/null 2>&1; }
  if [ -n "$(claude_bin)" ]; then res "Claude Code installed"; owner "log in once: open Terminal, run 'claude', /login"; else res "Claude Code install FAILED"; FAIL=1; fi
}

claude_on() {
  local bin; bin="$(claude_bin)"
  [ -z "$bin" ] && { res "Claude Code not installed - send remote_setup"; FAIL=1; return; }
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  # `script` gives remote-control a pseudo-terminal under launchd; KeepAlive restarts it, RunAtLoad = after reboot/login.
  cat > "$RC_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$RC_LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/script</string><string>-q</string><string>/dev/null</string>
    <string>$bin</string><string>remote-control</string><string>--name</string><string>$DEVICE</string>
    <string>--permission-mode</string><string>bypassPermissions</string>
  </array>
  <key>WorkingDirectory</key><string>$WORK</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$PATH</string><key>HOME</key><string>$HOME</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$RC_LOG</string>
  <key>StandardErrorPath</key><string>$RC_LOG</string>
</dict></plist>
EOF
  launchctl bootout "gui/$UIDN/$RC_LABEL" 2>/dev/null
  launchctl bootstrap "gui/$UIDN" "$RC_PLIST" 2>/dev/null
  sleep 8
  if launchctl print "gui/$UIDN/$RC_LABEL" 2>/dev/null | grep -q "state = running"; then
    res "always-on Claude running: remote-control '$DEVICE' in $WORK (launchd $RC_LABEL)"
  else
    res "always-on Claude NOT running - tail: $(tail -c 160 "$RC_LOG" 2>/dev/null | tr '\n' ' ')"; FAIL=1
  fi
  owner "if claude.ai/code does not list '$DEVICE': in Terminal run 'cd \"$WORK\" && claude' once (login + trust folder)"
}

claude_off() {
  launchctl bootout "gui/$UIDN/$RC_LABEL" 2>/dev/null
  rm -f "$RC_PLIST"
  pkill -f "claude remote-control" 2>/dev/null
  res "always-on Claude stopped (launchd agent removed; claude_on brings it back)"
}

case "$CMD" in
  remote_setup)
    install_rustdesk && configure_rustdesk
    tailscale_check; no_sleep; install_claude; claude_on
    owner "macOS: System Settings > Privacy & Security > Screen Recording + Accessibility > allow RustDesk (needs a click on the Mac once)"
    ;;
  remote_on) rustdesk_on ;;
  remote_off) rustdesk_off ;;
  claude_on) claude_on ;;
  claude_off) claude_off ;;
  *) res "unknown command '$CMD'"; FAIL=1 ;;
esac
exit $FAIL
