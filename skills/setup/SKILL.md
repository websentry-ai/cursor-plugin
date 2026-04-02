---
name: setup
description: Configure Unbound AI credentials and verify connectivity for the Cursor plugin. Use when setting up for the first time, reconfiguring with a new API key, or diagnosing connectivity issues.
user-invocable: true
---

# Unbound Setup

You are helping the user configure the Unbound AI plugin for Cursor. Hooks are bundled with the plugin and work immediately upon install. The setup script handles browser OAuth, API key persistence, and restarting Cursor.

---

## Step 1 — Check current state

Run this command to check whether the API key is already configured:

```bash
echo "${UNBOUND_CURSOR_API_KEY:0:8}..."
```

**If the variable is unset or empty**, proceed to Step 2.

**If the variable is already set**, tell the user the key is configured (show only the first 8 characters + `...`). Ask them to choose:
1. **Verify** — test connectivity with the existing key (jump to Step 4)
2. **Reconfigure** — replace with a new key (proceed to Step 2)
3. **Exit** — nothing to do

---

## Step 2 — Run the setup script

Run the setup script — it handles browser auth, API key persistence, and restarting Cursor:

```bash
python3 "${CURSOR_PLUGIN_ROOT}/scripts/setup.py" --domain gateway.getunbound.ai
```

The script will:
1. Open a browser for authentication
2. Save `UNBOUND_CURSOR_API_KEY` to the user's shell RC file
3. Restart Cursor

Check the exit code:
- **Exit code 0**: Setup succeeded.
- **Non-zero exit code**: Setup failed. Show the script's output to the user and offer to retry.

**Security property:** The API key never appears in chat, bash commands, or terminal output. It exists only inside the setup script's process memory and the RC file on disk.

---

## Step 3 — Load the new key into the current shell

The setup script wrote the key to the RC file but it is not yet available in this shell session. Source the RC file so the connectivity check can use it:

```bash
source <RC_FILE>
```

Use the same RC file the setup script reported (shown in its "Setup Complete!" output). The mapping is:

| OS | Shell | RC file |
|---|---|---|
| macOS | zsh | `~/.zprofile` |
| macOS | bash | `~/.bash_profile` |
| Linux | zsh | `~/.zshrc` |
| Linux | bash | `~/.bashrc` |

---

## Step 4 — Verify connectivity

Run:

```bash
curl -fsSL -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $UNBOUND_CURSOR_API_KEY" \
  https://api.getunbound.ai/v1/models
```

Interpret the result:

| HTTP code | Meaning | Action |
|---|---|---|
| `200` | Key is valid and API is reachable | Proceed to Step 5 |
| `401` | Key is invalid or expired | Tell the user the key was rejected. Offer to retry from Step 2. |
| `403` | Key exists but lacks required scope | Tell the user to create a new key with the correct scope. |
| anything else / curl error | Network issue or API unreachable | Warn the user. The plugin will **fail open** (allow all) until connectivity is restored. Still proceed to Step 5. |

---

## Step 5 — Show success summary

Print a summary like this (adapt `<RC_FILE>` to the actual RC file for the user's shell):

```
UNBOUND_CURSOR_API_KEY saved to <RC_FILE>
API connectivity verified (HTTP 200)
Unbound plugin is active — hooks are bundled and ready

What happens next:
  - Shell and MCP executions are checked against your Unbound policies
  - User prompts are scanned for DLP / NSFW / jailbreak guardrails
  - File reads, edits, and agent responses are audited
  - Session data streams to your Unbound dashboard for analytics

To view your policies and guardrails: https://app.getunbound.ai
```

If connectivity failed, end with:

```
API unreachable — plugin installed but running in fail-open mode.
    All actions will be allowed until connectivity is restored.
    Check your API key and network, then run /unbound-cursor:setup again.
```

---

## Re-setup guard

If the user chose **Reconfigure** in Step 1, confirm before overwriting:

> "This will replace your existing Unbound API key. Continue? (yes/no)"

Only proceed if they confirm. If they say no, exit gracefully.

---

## Error handling

- If any shell command fails, show the exact error and suggest a manual fix.
- Never exit silently — always tell the user what happened and what to do next.
- If the setup script fails, show the output and offer to retry.
