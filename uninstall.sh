#!/usr/bin/env bash
# Unbound Cursor Plugin — Uninstaller
#
# Removes downloaded hooks, scripts, env var, and log files.
set -euo pipefail

PLUGIN_PATH="$(cd "$(dirname "$0")" && pwd)"

echo "============================================================"
echo "  Unbound Cursor Plugin — Uninstaller"
echo "============================================================"
echo ""

# Remove env var + downloaded hooks.json + unbound.py
python3 "${PLUGIN_PATH}/scripts/setup.py" --clear

# Clean up log files
rm -f ~/.cursor/hooks/agent-audit.log
rm -f ~/.cursor/hooks/error.log
rm -f ~/.unbound/logs/debug.jsonl
rm -f ~/.unbound/logs/offline-events.jsonl
rm -f ~/.unbound/logs/trace.log
echo "Cleaned up log files"

echo ""
echo "============================================================"
echo "  Uninstall complete! Restart Cursor."
echo "============================================================"
