#!/usr/bin/env bash
# watchdog.sh — keep the Telegram bridge alive.
#
# The bridge is a single Python process. A PyObjC C-level crash (or an OOM / macOS
# jetsam kill) can take the whole process down with no Python-level recovery. This
# loop pings the bridge every ~30s and restarts it via start-bridge.sh if it's gone.
# It is intentionally EXTERNAL to the bridge so it survives a bridge crash.
#
# Usage — run detached (survives the session until reboot):
#   nohup scripts/watchdog.sh >> /tmp/tg_watchdog.log 2>&1 &
#   disown
#
# Or in the foreground (Ctrl-C to stop):
#   scripts/watchdog.sh
#
# Stop the watchdog itself:
#   pkill -f watchdog.sh
#
# Tunable: WATCHDOG_INTERVAL_S (default 30).
set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INTERVAL="${WATCHDOG_INTERVAL_S:-30}"

log() { echo "[$(date '+%F %T')] $*"; }

log "watchdog started — checking telegram_bridge every ${INTERVAL}s"

while true; do
  if pgrep -f 'telegram_bridge\.py' >/dev/null 2>&1; then
    : # alive
  else
    log "telegram_bridge NOT running — restarting via start-bridge.sh"
    "$ROOT_DIR/scripts/start-bridge.sh" >> /tmp/tg_watchdog.log 2>&1 \
      || log "start-bridge.sh exited $?"
    sleep 2
    if pgrep -f 'telegram_bridge\.py' >/dev/null 2>&1; then
      log "restart OK (pid $(pgrep -f 'telegram_bridge\.py' | head -1))"
    else
      log "restart FAILED — will retry next cycle"
    fi
  fi
  sleep "$INTERVAL"
done
