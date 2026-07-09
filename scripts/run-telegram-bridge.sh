#!/usr/bin/env bash
# Launch the Telegram bridge with the project venv (unbuffered). This is the
# single entry point used by the launchd plist AND by manual `nohup` launches.
#
# cd into agents/ first so the bridge's relative imports (agent_core, ax_tree,
# ...) resolve the same way they do when run by hand.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR/agents"

exec "$PROJECT_DIR/.venv/bin/python" -u telegram_bridge.py
