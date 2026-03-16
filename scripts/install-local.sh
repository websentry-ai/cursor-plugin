#!/usr/bin/env bash
# Install the Unbound Cursor plugin locally into Cursor's plugin cache.
#
# This simulates what the marketplace does: copies plugin files to the cache,
# registers in installed_plugins.json, and enables in settings.json.
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

CACHE_DIR="$HOME/.cursor/plugins/cache/local/${PLUGIN_NAME}/${PLUGIN_VERSION}"
INSTALLED_JSON="$HOME/.cursor/plugins/installed_plugins.json"
SETTINGS_JSON="$HOME/.cursor/settings.json"

# ── Uninstall ─────────────────────────────────────────────────
if [[ "${1:-}" == "--remove" ]]; then
    echo "Removing ${PLUGIN_KEY}..."

    # Remove from cache
    rm -rf "$CACHE_DIR"

    # Remove from installed_plugins.json
    if [ -f "$INSTALLED_JSON" ]; then
        python3 -c "
import json, sys
with open('$INSTALLED_JSON') as f:
    data = json.load(f)
data.get('plugins', {}).pop('$PLUGIN_KEY', None)
with open('$INSTALLED_JSON', 'w') as f:
    json.dump(data, f, indent=2)
print('Removed from installed_plugins.json')
"
    fi

    # Remove from settings.json
    if [ -f "$SETTINGS_JSON" ]; then
        python3 -c "
import json
with open('$SETTINGS_JSON') as f:
    data = json.load(f)
data.get('enabledPlugins', {}).pop('$PLUGIN_KEY', None)
with open('$SETTINGS_JSON', 'w') as f:
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

# 1. Copy plugin files to cache
rm -rf "$CACHE_DIR"
mkdir -p "$CACHE_DIR"
cp -R "$PLUGIN_SOURCE/.cursor-plugin" "$CACHE_DIR/"
cp -R "$PLUGIN_SOURCE/hooks" "$CACHE_DIR/"
cp -R "$PLUGIN_SOURCE/rules" "$CACHE_DIR/"
cp -R "$PLUGIN_SOURCE/skills" "$CACHE_DIR/"
cp -R "$PLUGIN_SOURCE/commands" "$CACHE_DIR/"
cp -R "$PLUGIN_SOURCE/scripts" "$CACHE_DIR/"
echo "Copied to $CACHE_DIR"

# 2. Register in installed_plugins.json
mkdir -p "$(dirname "$INSTALLED_JSON")"
python3 -c "
import json, os
from datetime import datetime, timezone

path = '$INSTALLED_JSON'
if os.path.exists(path):
    with open(path) as f:
        data = json.load(f)
else:
    data = {'version': 2, 'plugins': {}}

now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
data['plugins']['$PLUGIN_KEY'] = [{
    'scope': 'user',
    'installPath': '$CACHE_DIR',
    'version': '$PLUGIN_VERSION',
    'installedAt': now,
    'lastUpdated': now,
}]

with open(path, 'w') as f:
    json.dump(data, f, indent=2)
print('Registered in installed_plugins.json')
"

# 3. Enable in settings.json
python3 -c "
import json, os

path = '$SETTINGS_JSON'
if os.path.exists(path):
    with open(path) as f:
        data = json.load(f)
else:
    data = {}

if 'enabledPlugins' not in data:
    data['enabledPlugins'] = {}

data['enabledPlugins']['$PLUGIN_KEY'] = True

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
