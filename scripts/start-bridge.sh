#!/usr/bin/env bash
# Start the Telegram bridge as a detached, always-on daemon (nohup).
#
# Why nohup and not launchd? The project lives under ~/Documents, which macOS
# protects with TCC. A process launched from your terminal INHERITS the
# terminal's Documents access (and keeps it — TCC "responsible process" is fixed
# at birth and survives reparenting). A launchd LaunchAgent does NOT inherit it
# (needs a one-time Full Disk Access grant; see the .plist). So nohup is the
# zero-config way to keep the bridge alive until the next reboot.
#
# Idempotent: if the bridge is already running, this reports it and exits.
# Single-instance guard inside the bridge also prevents a 2nd poller (409).
#
# Usage:  scripts/start-bridge.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${1:-}" == "--restart" ]]; then
  pkill -f "telegram_bridge\.py" 2>/dev/null || true
  sleep 1
fi

if pgrep -f "telegram_bridge\.py" >/dev/null 2>&1; then
  echo "telegram_bridge already running (pid $(pgrep -f 'telegram_bridge\.py' | tr '\n' ' '))."
  exit 0
fi

cd "$PROJECT_DIR/agents"
: > /tmp/tg_bridge.log   # fresh log per launch
nohup "$PROJECT_DIR/.venv/bin/python" -u telegram_bridge.py >> /tmp/tg_bridge.log 2>&1 &
disown
sleep 1
PID="$(pgrep -f 'telegram_bridge\.py' | head -1 || true)"
echo "telegram_bridge started (pid ${PID:-?}) — logs: /tmp/tg_bridge.log"
