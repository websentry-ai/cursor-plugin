#!/usr/bin/env bash
# Unbound Cursor Plugin — Uninstaller
#
# Removes user-level hooks and clears UNBOUND_CURSOR_API_KEY.
set -euo pipefail

PLUGIN_PATH="$(cd "$(dirname "$0")" && pwd)"
HOOKS_FILE="$HOME/.cursor/hooks.json"

echo "============================================================"
echo "  Unbound Cursor Plugin — Uninstaller"
echo "============================================================"
echo ""

# Remove hooks
if [ -f "$HOOKS_FILE" ]; then
    rm "$HOOKS_FILE"
    echo "Removed $HOOKS_FILE"
else
    echo "No hooks file found at $HOOKS_FILE"
fi

# Remove API key from shell RC file
python3 "${PLUGIN_PATH}/scripts/setup.py" --clear

# Clean up log files
rm -f ~/.cursor/hooks/agent-audit.log
rm -f ~/.cursor/hooks/error.log
rm -f ~/.unbound/logs/debug.jsonl
rm -f ~/.unbound/logs/offline-events.jsonl
echo "Cleaned up log files"

echo ""
echo "============================================================"
echo "  Uninstall complete! Restart Cursor."
echo "============================================================"
