#!/usr/bin/env bash
# Unbound Cursor Plugin — Single-command installer
#
# Usage:
#   ./install.sh              Full install (hooks + browser auth)
#   ./install.sh --hooks-only Install hooks only, skip API key setup
#
# What it does:
#   1. Writes user-level hooks to ~/.cursor/hooks.json
#   2. Runs browser OAuth to obtain UNBOUND_CURSOR_API_KEY
#   3. Saves the key to your shell RC file
#
# After install, restart Cursor to activate.
set -euo pipefail

PLUGIN_PATH="$(cd "$(dirname "$0")" && pwd)"
HOOKS_FILE="$HOME/.cursor/hooks.json"
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

# ── Step 1: Install user-level hooks ──────────────────────────

if [ -f "$HOOKS_FILE" ]; then
    echo "Found existing $HOOKS_FILE"
    echo "Backing up to ${HOOKS_FILE}.bak"
    cp "$HOOKS_FILE" "${HOOKS_FILE}.bak"
fi

mkdir -p "$(dirname "$HOOKS_FILE")"
cat > "$HOOKS_FILE" << EOF
{
  "version": 1,
  "hooks": {
    "sessionStart": [
      {
        "command": "python3 ${PLUGIN_PATH}/scripts/session-start.py",
        "timeout": 5
      }
    ],
    "preToolUse": [
      {
        "command": "python3 ${PLUGIN_PATH}/scripts/hook-handler.py preToolUse",
        "timeout": 10
      }
    ],
    "postToolUse": [
      {
        "command": "python3 ${PLUGIN_PATH}/scripts/hook-handler.py postToolUse",
        "timeout": 60
      }
    ],
    "beforeSubmitPrompt": [
      {
        "command": "python3 ${PLUGIN_PATH}/scripts/hook-handler.py beforeSubmitPrompt",
        "timeout": 60
      }
    ],
    "sessionEnd": [
      {
        "command": "python3 ${PLUGIN_PATH}/scripts/hook-handler.py sessionEnd",
        "timeout": 60
      }
    ]
  }
}
EOF

echo "Hooks installed at $HOOKS_FILE"
echo ""

# ── Step 2: API key setup via browser OAuth ───────────────────

if [ "$HOOKS_ONLY" = true ]; then
    echo "Skipping API key setup (--hooks-only)"
else
    if [ -n "${UNBOUND_CURSOR_API_KEY:-}" ]; then
        echo "UNBOUND_CURSOR_API_KEY is already set (${UNBOUND_CURSOR_API_KEY:0:8}...)"
        echo -n "Replace with a new key? [y/N] "
        read -r reply
        if [[ ! "$reply" =~ ^[Yy]$ ]]; then
            echo "Keeping existing key."
            echo ""
            echo "============================================================"
            echo "  Installation complete! Restart Cursor to activate."
            echo "============================================================"
            exit 0
        fi
    fi

    echo "Starting browser authentication..."
    python3 "${PLUGIN_PATH}/scripts/setup.py" --domain gateway.getunbound.ai
fi

# ── Done ──────────────────────────────────────────────────────

echo ""
echo "============================================================"
echo "  Installation complete! Restart Cursor to activate."
echo "============================================================"
echo ""
echo "Next steps:"
echo "  1. Close and reopen Cursor"
echo "  2. Start a conversation — the Unbound plugin will be active"
echo ""
echo "To uninstall later:  ./uninstall.sh"
