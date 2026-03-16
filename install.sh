#!/usr/bin/env bash
# Unbound Cursor Plugin — Single-command installer
#
# Usage:
#   ./install.sh
#
# What it does:
#   1. Runs browser OAuth to obtain UNBOUND_CURSOR_API_KEY
#   2. Saves the key to your shell RC file
#   3. Restarts Cursor
#
# Hooks are bundled in the plugin and registered automatically by Cursor.
set -euo pipefail

PLUGIN_PATH="$(cd "$(dirname "$0")" && pwd)"

echo "============================================================"
echo "  Unbound Cursor Plugin — Installer"
echo "============================================================"
echo ""

if [ -n "${UNBOUND_CURSOR_API_KEY:-}" ]; then
    echo "UNBOUND_CURSOR_API_KEY is already set (${UNBOUND_CURSOR_API_KEY:0:8}...)"
    echo -n "Replace with a new key? [y/N] "
    read -r reply
    if [[ ! "$reply" =~ ^[Yy]$ ]]; then
        echo "Keeping existing key."
        echo ""
        echo "============================================================"
        echo "  Done! Restart Cursor to activate."
        echo "============================================================"
        exit 0
    fi
fi

echo "Starting setup..."
python3 "${PLUGIN_PATH}/scripts/setup.py" --domain gateway.getunbound.ai

echo ""
echo "============================================================"
echo "  Installation complete! Restart Cursor to activate."
echo "============================================================"
echo ""
echo "To uninstall later:  ./uninstall.sh"
