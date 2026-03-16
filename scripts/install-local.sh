#!/usr/bin/env bash
# Install the Unbound Cursor plugin for local development.
#
# Cursor plugin discovery:
#   - Plugin files:    ~/.cursor/plugins/<plugin-name>/
#   - Registration:    ~/.claude/plugins/installed_plugins.json
#   - Enabled flag:    ~/.claude/settings.json  (under enabledPlugins)
#
# Usage:
#   ./scripts/install-local.sh           Install/update the plugin
#   ./scripts/install-local.sh --remove  Uninstall the plugin
#
# After running, restart Cursor (Cmd+Q → reopen).
set -euo pipefail

PLUGIN_SOURCE="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN_NAME="unbound-cursor"
PLUGIN_KEY="${PLUGIN_NAME}@local"
PLUGIN_VERSION="1.0.0"

# Plugin files live under ~/.cursor/plugins/<name>
INSTALL_DIR="$HOME/.cursor/plugins/${PLUGIN_NAME}"
# Cursor reads registration + enablement from ~/.claude/
INSTALLED_JSON="$HOME/.claude/plugins/installed_plugins.json"
SETTINGS_JSON="$HOME/.claude/settings.json"

# ── Uninstall ─────────────────────────────────────────────────
if [[ "${1:-}" == "--remove" ]]; then
    echo "Removing ${PLUGIN_KEY}..."

    # Remove plugin files
    rm -rf "$INSTALL_DIR"

    # Remove from installed_plugins.json
    if [ -f "$INSTALLED_JSON" ]; then
        _INSTALLED_JSON="$INSTALLED_JSON" _PLUGIN_KEY="$PLUGIN_KEY" python3 -c "
import json, os
path = os.environ['_INSTALLED_JSON']
key = os.environ['_PLUGIN_KEY']
with open(path) as f:
    data = json.load(f)
data.get('plugins', {}).pop(key, None)
with open(path, 'w') as f:
    json.dump(data, f, indent=2)
print('Removed from installed_plugins.json')
"
    fi

    # Remove from settings.json
    if [ -f "$SETTINGS_JSON" ]; then
        _SETTINGS_JSON="$SETTINGS_JSON" _PLUGIN_KEY="$PLUGIN_KEY" python3 -c "
import json, os
path = os.environ['_SETTINGS_JSON']
key = os.environ['_PLUGIN_KEY']
with open(path) as f:
    data = json.load(f)
data.get('enabledPlugins', {}).pop(key, None)
with open(path, 'w') as f:
    json.dump(data, f, indent=2)
print('Removed from settings.json')
"
    fi

    echo ""
    echo "Done. Restart Cursor to apply."
    exit 0
fi

# ── Install ───────────────────────────────────────────────────
echo "Installing ${PLUGIN_KEY} from ${PLUGIN_SOURCE}..."

# 1. Copy plugin files to ~/.cursor/plugins/<name>/
rm -rf "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cp -R "$PLUGIN_SOURCE/.cursor-plugin" "$INSTALL_DIR/"
cp -R "$PLUGIN_SOURCE/hooks" "$INSTALL_DIR/"
cp -R "$PLUGIN_SOURCE/rules" "$INSTALL_DIR/"
cp -R "$PLUGIN_SOURCE/skills" "$INSTALL_DIR/"
cp -R "$PLUGIN_SOURCE/commands" "$INSTALL_DIR/"
cp -R "$PLUGIN_SOURCE/scripts" "$INSTALL_DIR/"
echo "Copied to $INSTALL_DIR"

# 2. Register in ~/.claude/plugins/installed_plugins.json
mkdir -p "$(dirname "$INSTALLED_JSON")"
_INSTALLED_JSON="$INSTALLED_JSON" _PLUGIN_KEY="$PLUGIN_KEY" _INSTALL_DIR="$INSTALL_DIR" _PLUGIN_VERSION="$PLUGIN_VERSION" python3 -c "
import json, os
from datetime import datetime, timezone

path = os.environ['_INSTALLED_JSON']
key = os.environ['_PLUGIN_KEY']
install_dir = os.environ['_INSTALL_DIR']
version = os.environ['_PLUGIN_VERSION']

if os.path.exists(path):
    with open(path) as f:
        data = json.load(f)
else:
    data = {'version': 2, 'plugins': {}}

now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
data['plugins'][key] = [{
    'scope': 'user',
    'installPath': install_dir,
    'version': version,
    'installedAt': now,
    'lastUpdated': now,
}]

with open(path, 'w') as f:
    json.dump(data, f, indent=2)
print('Registered in installed_plugins.json')
"

# 3. Enable in ~/.claude/settings.json
_SETTINGS_JSON="$SETTINGS_JSON" _PLUGIN_KEY="$PLUGIN_KEY" python3 -c "
import json, os

path = os.environ['_SETTINGS_JSON']
key = os.environ['_PLUGIN_KEY']

if os.path.exists(path):
    with open(path) as f:
        data = json.load(f)
else:
    data = {}

if 'enabledPlugins' not in data:
    data['enabledPlugins'] = {}

data['enabledPlugins'][key] = True

with open(path, 'w') as f:
    json.dump(data, f, indent=2)
print('Enabled in settings.json')
"

echo ""
echo "============================================================"
echo "  ${PLUGIN_NAME} installed locally!"
echo "============================================================"
echo ""
echo "Next steps:"
echo "  1. Quit Cursor completely (Cmd+Q)"
echo "  2. Reopen Cursor"
echo "  3. Start a new conversation — the plugin should be active"
echo ""
echo "To update after code changes:  ./scripts/install-local.sh"
echo "To remove:                     ./scripts/install-local.sh --remove"
