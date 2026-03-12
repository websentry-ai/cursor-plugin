#!/usr/bin/env bash
# Unbound Cursor Plugin — Single-command installer
#
# Usage:
#   ./install.sh              Full install (hooks + browser auth)
#   ./install.sh --hooks-only Download hooks only, skip API key setup
#
# What it does:
#   1. Downloads hooks.json and unbound.py from websentry-ai/setup
#   2. Runs browser OAuth to obtain UNBOUND_CURSOR_API_KEY
#   3. Saves the key to your shell RC file
#   4. Restarts Cursor
#
# After install, restart Cursor to activate.
set -euo pipefail

PLUGIN_PATH="$(cd "$(dirname "$0")" && pwd)"
HOOKS_ONLY=false

for arg in "$@"; do
    case "$arg" in
        --hooks-only) HOOKS_ONLY=true ;;
    esac
done

echo "============================================================"
echo "  Unbound Cursor Plugin — Installer"
echo "============================================================"
echo ""

if [ "$HOOKS_ONLY" = true ]; then
    python3 "${PLUGIN_PATH}/scripts/setup.py" --hooks-only
else
    if [ -n "${UNBOUND_CURSOR_API_KEY:-}" ]; then
        echo "UNBOUND_CURSOR_API_KEY is already set (${UNBOUND_CURSOR_API_KEY:0:8}...)"
        echo -n "Replace with a new key? [y/N] "
        read -r reply
        if [[ ! "$reply" =~ ^[Yy]$ ]]; then
            echo "Keeping existing key. Downloading latest hooks..."
            python3 "${PLUGIN_PATH}/scripts/setup.py" --hooks-only
            echo ""
            echo "============================================================"
            echo "  Installation complete! Restart Cursor to activate."
            echo "============================================================"
            exit 0
        fi
    fi

    echo "Starting setup..."
    python3 "${PLUGIN_PATH}/scripts/setup.py" --domain gateway.getunbound.ai
fi

echo ""
echo "============================================================"
echo "  Installation complete! Restart Cursor to activate."
echo "============================================================"
echo ""
echo "To uninstall later:  ./uninstall.sh"
