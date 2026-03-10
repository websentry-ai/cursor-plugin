# Enterprise MDM Deployment

This directory contains the hooks template for enforcing the Unbound plugin across a fleet.

## What it does

`hooks.json` is read by Cursor from a system-wide path that regular users cannot modify. Enterprise-level hooks have the highest priority and override Team, Project, and User hooks.

| OS | Path |
|---|---|
| macOS | `/Library/Application Support/Cursor/hooks.json` |
| Linux | `/etc/cursor/hooks.json` |
| Windows | `C:\ProgramData\Cursor\hooks.json` |

## Deployment steps

### 1. Deploy hooks.json

Copy `hooks.json.tmpl` to the system path for your target OS and rename it to `hooks.json`.

macOS (run as root or via MDM):

```bash
mkdir -p "/Library/Application Support/Cursor"
cp hooks.json.tmpl "/Library/Application Support/Cursor/hooks.json"
```

Linux (run as root):

```bash
mkdir -p /etc/cursor
cp hooks.json.tmpl /etc/cursor/hooks.json
```

### 2. Set UNBOUND_CURSOR_API_KEY for each user

The plugin reads `UNBOUND_CURSOR_API_KEY` from the environment. This must be set per user.

**Option A — MDM-issued device API key (recommended)**

Use the Unbound MDM provisioning endpoint to fetch a per-device key at enrollment time:

```
GET https://api.getunbound.ai/api/v1/automations/mdm/get_application_api_key/
    ?serial_number=<DEVICE_SERIAL>
    &app_type=cursor
```

Requires an Unbound MDM auth key. See your Unbound dashboard under Settings > MDM.

**Option B — Shared fleet key**

Set a single key for all users via a login script or MDM configuration profile:

```bash
# /etc/profile.d/unbound.sh  (Linux)
export UNBOUND_CURSOR_API_KEY="<YOUR_KEY>"
```

macOS: deploy a Configuration Profile (`.mobileconfig`) that sets the env var, or add the export to `/etc/zshenv`.

### 3. Alternative: Team Marketplace

Cursor supports Team marketplaces (available on Teams and Enterprise plans) as an alternative to MDM:

1. Go to your Cursor Team Dashboard > Settings > Plugins
2. Import the GitHub repository: `websentry-ai/cursor-extension`
3. Set the plugin as **required** — it auto-installs for all team members
4. Users cannot disable required plugins

This approach is simpler than MDM but requires a Cursor Teams or Enterprise subscription.

### 4. Verify

On an enrolled machine, open Cursor and start a new conversation. The AI should either:

- Report that Unbound is active (if `UNBOUND_CURSOR_API_KEY` is set)
- Prompt the user to run setup (if `UNBOUND_CURSOR_API_KEY` is missing)

Test policy enforcement:

```
- Block policy: create a BLOCK rule in your Unbound dashboard > try running `rm -rf /` > should be blocked
- DLP guardrail: enable DLP > type a prompt containing an SSN > should be blocked
- Analytics: run any command > check your Unbound dashboard for the event
```
