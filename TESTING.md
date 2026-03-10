# Local Testing Guide

How to test the Unbound Cursor plugin on your local machine before marketplace submission.

---

## Prerequisites

- Cursor IDE installed (with hooks support)
- Python 3.8+

---

## Quick start (single command)

Clone the repo and run the installer:

```bash
git clone https://github.com/websentry-ai/cursor-extension.git
cd cursor-extension
./install.sh
```

This does everything:
1. Installs user-level hooks at `~/.cursor/hooks.json` (applies to all projects)
2. Opens your browser for Unbound authentication
3. Saves `UNBOUND_CURSOR_API_KEY` to your shell RC file

After it finishes, **restart Cursor** and the plugin is active.

---

## Alternative: AI-guided setup

If hooks are already installed (via `install.sh --hooks-only` or marketplace):

1. Open Cursor and start a new conversation
2. The AI will detect that `UNBOUND_CURSOR_API_KEY` is missing and offer to set it up
3. It runs the setup script for you — browser opens, you authenticate, done
4. Restart Cursor

---

## Uninstall

```bash
./uninstall.sh
```

Removes hooks, API key, and log files. Restart Cursor after.

---

## How local testing works

Cursor doesn't have a `cursor plugin install --path` command. Instead, `install.sh` writes **user-level hooks** (`~/.cursor/hooks.json`) that apply to all projects. The hook commands point to scripts in this repo via absolute paths.

For marketplace installs, hooks use `${CURSOR_PLUGIN_ROOT}` instead.

---

## Test Plan

### Test A: sessionStart — API key detection

**Test A.1: Missing key**

1. Ensure `UNBOUND_CURSOR_API_KEY` is NOT set:
   ```bash
   unset UNBOUND_CURSOR_API_KEY
   ```
2. Restart Cursor (hooks inherit env from the parent shell)
3. Open the test project and start a new Agent conversation
4. **Expected**: The AI should proactively tell you that Unbound setup is needed and offer to guide you through the process

**Test A.2: Key present**

1. Set the key:
   ```bash
   export UNBOUND_CURSOR_API_KEY="your-test-key"
   ```
2. Restart Cursor
3. Start a new conversation
4. **Expected**: No setup prompt. The AI should behave normally. Check debug log:
   ```bash
   tail -1 ~/.unbound/logs/debug.jsonl | python3 -m json.tool
   ```

**Test A.3: Background agent**

1. If Cursor supports background agents/tasks, trigger one
2. **Expected**: No setup prompt for background agents (they skip the key check)

---

### Test B: preToolUse — Command policy enforcement

**Test B.1: Allow (no policy violation)**

1. Ask the AI to run a safe command: "list files in the current directory"
2. **Expected**: Command runs normally
3. Verify in debug log:
   ```bash
   grep '"event":"preToolUse"' ~/.unbound/logs/debug.jsonl | tail -1 | python3 -m json.tool
   ```

**Test B.2: Block (policy violation)**

1. In your Unbound dashboard (https://app.getunbound.ai), create a BLOCK rule for `rm -rf`
2. Ask the AI: "run rm -rf /tmp/test"
3. **Expected**: The command is blocked. Cursor should show the block reason from the Unbound API

**Test B.3: Fail open (API unreachable)**

1. Temporarily set an invalid key:
   ```bash
   export UNBOUND_CURSOR_API_KEY="invalid-key-12345"
   ```
2. Restart Cursor, ask the AI to run `ls`
3. **Expected**: Command runs normally (fail open). Check error log:
   ```bash
   cat ~/.cursor/hooks/error.log
   ```

---

### Test C: beforeSubmitPrompt — DLP / Guardrails

**Test C.1: Clean prompt**

1. Type a normal prompt: "What is 2 + 2?"
2. **Expected**: Prompt goes through normally

**Test C.2: DLP block**

1. In your Unbound dashboard, enable DLP guardrails
2. Type a prompt containing a fake SSN: "My SSN is 123-45-6789"
3. **Expected**: Prompt is blocked. Cursor should show a message like "PII detected"

**Test C.3: Blocked prompt not logged**

1. Trigger a DLP block (as above)
2. Check audit log — the blocked prompt should NOT appear:
   ```bash
   grep "123-45-6789" ~/.cursor/hooks/agent-audit.log
   # Should return nothing
   ```

---

### Test D: postToolUse — Audit logging

**Test D.1: Tool use is logged**

1. Ask the AI to read a file or run a command
2. Check the audit log:
   ```bash
   tail -5 ~/.cursor/hooks/agent-audit.log | python3 -m json.tool
   ```
3. **Expected**: Entry with `hook_event_name: "postToolUse"`, including `tool_name`, `tool_input`, and Cursor-specific fields like `duration`

**Test D.2: No stdout output**

1. Check the Hooks output channel in Cursor Settings
2. **Expected**: No output from postToolUse hooks (they're silent, audit-only)

---

### Test E: sessionEnd — Exchange submission

**Test E.1: Successful exchange**

1. Have a short conversation (ask a question, let the AI use a tool)
2. Close the conversation or start a new one (triggers sessionEnd)
3. Check the Unbound dashboard — the session should appear
4. Check that audit log was cleaned up for this session:
   ```bash
   cat ~/.cursor/hooks/agent-audit.log
   ```

**Test E.2: Offline fallback**

1. Set an invalid key to simulate API failure:
   ```bash
   export UNBOUND_CURSOR_API_KEY="invalid-key"
   ```
2. Restart Cursor, have a conversation, close it
3. Check offline log:
   ```bash
   cat ~/.unbound/logs/offline-events.jsonl | python3 -m json.tool
   ```
4. **Expected**: Exchange data saved to offline log for later retry

---

### Test F: Setup flow

**Test F.1: Single-command install**

1. Run:
   ```bash
   ./install.sh
   ```
2. **Expected**: Hooks written to `~/.cursor/hooks.json`, browser opens for auth, key saved to RC file
3. Restart Cursor and verify the plugin is active

**Test F.2: AI-guided setup (no key)**

1. Run hooks-only install:
   ```bash
   ./install.sh --hooks-only
   ```
2. Unset the key:
   ```bash
   unset UNBOUND_CURSOR_API_KEY
   ```
3. Restart Cursor, start a new conversation
4. **Expected**: AI detects missing key, offers to run setup, browser opens, key obtained

**Test F.3: Uninstall**

1. Run:
   ```bash
   ./uninstall.sh
   ```
2. **Expected**: Hooks removed, `UNBOUND_CURSOR_API_KEY` cleared from RC file, logs cleaned up

**Test F.4: On-demand setup rule**

1. With the plugin's `rules/` directory accessible, start a conversation
2. Ask: "How do I set up the Unbound security plugin?"
3. **Expected**: Cursor's AI should reference the `setup-guide.mdc` rule and walk you through the process

---

## Debugging

### Log files

| File | What to check |
|---|---|
| `~/.unbound/logs/debug.jsonl` | Raw stdin for every hook invocation — verify payloads |
| `~/.cursor/hooks/agent-audit.log` | Audit trail — verify events are being logged |
| `~/.cursor/hooks/error.log` | API errors — check for connectivity issues |
| `~/.unbound/logs/offline-events.jsonl` | Failed exchanges — check if API is unreachable |

### Cursor's built-in tools

1. **Hooks tab** (Cursor Settings > Hooks) — shows configured hooks and execution status
2. **Hooks output channel** — shows stderr/errors from hook scripts

### Manual hook testing

You can test a hook script directly by piping JSON to it:

```bash
# Test preToolUse
echo '{"conversation_id":"test-123","tool_name":"Shell","tool_input":{"command":"ls"},"model":"gpt-4"}' | \
  UNBOUND_CURSOR_API_KEY="your-key" python3 scripts/hook-handler.py preToolUse

# Test beforeSubmitPrompt
echo '{"conversation_id":"test-123","prompt":"hello world","model":"gpt-4"}' | \
  UNBOUND_CURSOR_API_KEY="your-key" python3 scripts/hook-handler.py beforeSubmitPrompt

# Test sessionStart (no key)
echo '{}' | python3 scripts/session-start.py

# Test sessionStart (with key)
echo '{}' | UNBOUND_CURSOR_API_KEY="your-key" python3 scripts/session-start.py

# Test sessionStart (background agent)
echo '{"is_background_agent":true}' | python3 scripts/session-start.py
```

### Clear all logs

```bash
rm -f ~/.cursor/hooks/agent-audit.log
rm -f ~/.cursor/hooks/error.log
rm -f ~/.unbound/logs/debug.jsonl
rm -f ~/.unbound/logs/offline-events.jsonl
```

---

## Automated tests

Run the unit test suite (no Cursor or API key needed):

```bash
pip install pytest
python3 -m pytest tests/ -v
```

All 113 tests should pass.

---

## Checklist

| # | Test | Status |
|---|---|---|
| A.1 | sessionStart — missing key prompts setup | |
| A.2 | sessionStart — present key shows "active" | |
| A.3 | sessionStart — background agent skips prompt | |
| B.1 | preToolUse — safe command allowed | |
| B.2 | preToolUse — blocked command denied | |
| B.3 | preToolUse — fail open on API error | |
| C.1 | beforeSubmitPrompt — clean prompt allowed | |
| C.2 | beforeSubmitPrompt — DLP blocks PII | |
| C.3 | beforeSubmitPrompt — blocked prompt not logged | |
| D.1 | postToolUse — tool use logged to audit | |
| D.2 | postToolUse — no stdout output | |
| E.1 | sessionEnd — exchange sent to dashboard | |
| E.2 | sessionEnd — offline fallback on failure | |
| F.1 | Install — single-command setup | |
| F.2 | Install — AI-guided setup flow | |
| F.3 | Uninstall — clean removal | |
| F.4 | Setup — on-demand rule guidance | |
