---
description: Configure Unbound AI credentials and verify connectivity. Use for first-time setup, reconfiguration, or troubleshooting. Works in both local Cursor and Cloud Agent environments.
---

# Unbound Setup

You are helping the user configure the Unbound AI plugin for Cursor. Hooks are bundled with the plugin and work immediately upon install. The setup flow differs between **local Cursor** (interactive desktop) and **Cloud Agents** (headless VMs on cursor.com/agents).

---

## Step 1 — Check current state

Run this command to check whether the API key is already configured:

```bash
echo "${UNBOUND_CURSOR_API_KEY:0:8}..."
```

**If the variable is unset or empty**, proceed to Step 1b to detect the environment.

**If the variable is already set**, tell the user the key is configured (show only the first 8 characters + `...`). Ask them to choose:
1. **Verify** — test connectivity with the existing key (jump to Step 4)
2. **Reconfigure** — replace with a new key (proceed to Step 1b)
3. **Exit** — nothing to do

---

## Step 1b — Detect environment

Determine whether you are running inside a Cloud Agent or a local Cursor desktop:

```bash
python3 -c "import webbrowser; webbrowser.open('data:,')" 2>&1 | head -5
```

- **If the browser opens or the command succeeds** → you are on a **local desktop**. Proceed to **Step 2a (Local setup)**.
- **If it fails with "No display" / "could not find or open browser"** → you are on a **Cloud Agent**. Proceed to **Step 2b (Cloud Agent setup)**.

---

## Step 2a — Local setup (browser OAuth)

Run the setup script — it handles browser auth, API key persistence, and restarting Cursor:

```bash
python3 "${CURSOR_PLUGIN_ROOT}/scripts/setup.py" --domain gateway.getunbound.ai
```

The script will:
1. Open a browser for authentication
2. Save `UNBOUND_CURSOR_API_KEY` to the user's shell RC file
3. Restart Cursor

Check the exit code:
- **Exit code 0**: Setup succeeded. Proceed to Step 3a.
- **Non-zero exit code**: Setup failed. Show the script's output to the user and offer to retry.

**Security property:** The API key never appears in chat, bash commands, or terminal output. It exists only inside the setup script's process memory and the RC file on disk.

---

## Step 2b — Cloud Agent setup (secrets via dashboard)

Cloud Agent VMs are headless and ephemeral — browser OAuth and shell RC files do not work. The API key must be provisioned through the **Cursor Dashboard** instead.

Tell the user:

```
Cloud Agent detected — browser-based setup is not available.
To configure Unbound for Cloud Agents:

1. Get your API key from https://app.getunbound.ai → Settings → API Keys
   (create one if you don't have it yet)

2. Add it as a Cloud Agent secret in the Cursor Dashboard:
   → cursor.com → Settings → Cloud Agents → Secrets
   → Name:  UNBOUND_CURSOR_API_KEY
   → Value: <your API key>
   → Scope: select your repo (or make it user-wide)

3. Start a new Cloud Agent session — the secret is injected
   as an environment variable automatically.

The plugin hooks will pick up the key from the environment on every run.
No shell RC files or Cursor restarts are needed.
```

After delivering these instructions, jump to **Step 4** to verify connectivity if the key is already present in the environment, or tell the user to come back after adding the secret.

---

## Step 3a — Load the new key into the current shell (local only)

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
| `401` | Key is invalid or expired | Tell the user the key was rejected. Offer to retry from Step 2a/2b. |
| `403` | Key exists but lacks required scope | Tell the user to create a new key with the correct scope. |
| anything else / curl error | Network issue or API unreachable | Warn the user. The plugin will **fail open** (allow all) until connectivity is restored. Still proceed to Step 5. |

---

## Step 5 — Show success summary

**Local setup** — print:

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

**Cloud Agent setup** — print:

```
UNBOUND_CURSOR_API_KEY detected in environment
API connectivity verified (HTTP 200)
Unbound plugin is active — hooks are bundled and ready

What happens next:
  - Shell and MCP executions are checked against your Unbound policies
  - User prompts are scanned for DLP / NSFW / jailbreak guardrails
  - File reads, edits, and agent responses are audited
  - Session data streams to your Unbound dashboard for analytics

The key is managed via Cursor Dashboard secrets and persists across Cloud Agent sessions.
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
- In Cloud Agent context, if the key is missing, always direct the user to the Cursor Dashboard secrets page rather than attempting browser OAuth.
