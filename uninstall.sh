#!/usr/bin/env bash
# Unbound Cursor Plugin — Uninstaller
#
# Removes UNBOUND_CURSOR_API_KEY and cleans up log files.
# The plugin itself should be uninstalled through Cursor's marketplace UI.
set -euo pipefail

PLUGIN_PATH="$(cd "$(dirname "$0")" && pwd)"

echo "============================================================"
echo "  Unbound Cursor Plugin — Uninstaller"
echo "============================================================"
echo ""

# Remove env var
python3 "${PLUGIN_PATH}/scripts/setup.py" --clear

# Clean up log files
rm -f ~/.cursor/hooks/agent-audit.log
rm -f ~/.cursor/hooks/error.log
echo "Cleaned up log files"

echo ""
echo "============================================================"
echo "  Uninstall complete! Restart Cursor."
echo "============================================================"
