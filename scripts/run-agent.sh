#!/usr/bin/env bash
# Phase 1 entry point — runs the deep-AX, number-based agent.
#
# Usage:
#   ./scripts/run-agent.sh "open Safari and search for hiking trails near Calgary"
#
# Requirements (one-time):
#   System Settings → Privacy & Security → Accessibility  → enable your terminal
#   System Settings → Privacy & Security → Screen Recording → enable your terminal

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PY="$ROOT_DIR/.venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
  echo "venv python not found at $VENV_PY"
  echo "Create it with:  python3.11 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

if [ "$#" -lt 1 ]; then
  echo 'Usage: ./scripts/run-agent.sh "<task>"'
  exit 1
fi

cd "$ROOT_DIR/agents"
exec "$VENV_PY" agent_core.py "$@"
