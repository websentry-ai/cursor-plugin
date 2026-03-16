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
1. Opens your browser for Unbound authentication
2. Saves `UNBOUND_CURSOR_API_KEY` to your shell RC file
3. Restarts Cursor

Hooks are bundled with the plugin and work immediately — no separate download needed.

---

## Alternative: Skill-guided setup

Run `/unbound-cursor:setup` in any Cursor conversation. The AI will walk you through the setup flow.

---

## Uninstall

```bash
./uninstall.sh
```

Removes the API key and log files. Restart Cursor after.

---

## How it works

The plugin bundles two files (sourced from [websentry-ai/setup](https://github.com/websentry-ai/setup)):
- `hooks/hooks.json` — registers 9 hooks for Cursor's lifecycle events
- `hooks/unbound.py` — single script that handles all hook events

All hooks point to this single `unbound.py` script via `${CURSOR_PLUGIN_ROOT}`. It reads the `UNBOUND_CURSOR_API_KEY` env var to authenticate with the Unbound API.

---

## Test Plan

### Test A: beforeShellExecution — Command policy

**Test A.1: Allow (no policy violation)**

1. Ask the AI to run a safe command: "list files in the current directory"
2. **Expected**: Command runs normally

**Test A.2: Block (policy violation)**

1. In your Unbound dashboard (https://app.getunbound.ai), create a BLOCK rule for `rm -rf`
2. Ask the AI: "run rm -rf /tmp/test"
3. **Expected**: The command is blocked. Cursor should show the block reason

**Test A.3: Fail open (API unreachable)**

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

### Test B: beforeSubmitPrompt — DLP / Guardrails

**Test B.1: Clean prompt**

1. Type a normal prompt: "What is 2 + 2?"
2. **Expected**: Prompt goes through normally

**Test B.2: DLP block**

1. In your Unbound dashboard, enable DLP guardrails
2. Type a prompt containing a fake SSN: "My SSN is 123-45-6789"
3. **Expected**: Prompt is blocked with exit code 2. Cursor should show "PII detected" or similar

---

### Test C: Audit logging

**Test C.1: Events are logged**

1. Have a conversation with tool use (shell commands, file reads)
2. Check the audit log:
   ```bash
   tail -5 ~/.cursor/hooks/agent-audit.log | python3 -m json.tool
   ```
3. **Expected**: Entries for `afterShellExecution`, `afterFileEdit`, `beforeReadFile`, `afterAgentResponse`

---

### Test D: stop — Exchange submission

**Test D.1: Successful exchange**

1. Have a short conversation, let the AI use tools
2. Close the conversation (triggers `stop` event)
3. Check the Unbound dashboard — the session should appear
4. Check that audit log was cleaned up:
   ```bash
   cat ~/.cursor/hooks/agent-audit.log
   ```

**Test D.2: Offline fallback**

1. Set an invalid key to simulate API failure
2. Restart Cursor, have a conversation, close it
3. **Expected**: Exchange data saved to offline log

---

### Test E: Setup flow

**Test E.1: Single-command install**

1. Run:
   ```bash
   ./install.sh
   ```
2. **Expected**: Browser opens for auth, key saved, Cursor restarts. Hooks are bundled and active immediately.

**Test E.2: Uninstall**

1. Run:
   ```bash
   ./uninstall.sh
   ```
2. **Expected**: `UNBOUND_CURSOR_API_KEY` cleared, logs cleaned

---

## Debugging

### Log files

| File | What to check |
|---|---|
| `~/.cursor/hooks/agent-audit.log` | Audit trail — verify events are being logged |
| `~/.cursor/hooks/error.log` | API errors — check for connectivity issues |

### Manual hook testing

You can test the bundled `unbound.py` directly by piping JSON to it:

```bash
# Test beforeShellExecution
echo '{"hook_event_name":"beforeShellExecution","conversation_id":"test-123","command":"ls","model":"gpt-4"}' | \
  UNBOUND_CURSOR_API_KEY="your-key" python3 hooks/unbound.py

# Test beforeSubmitPrompt
echo '{"hook_event_name":"beforeSubmitPrompt","conversation_id":"test-123","prompt":"hello world","model":"gpt-4"}' | \
  UNBOUND_CURSOR_API_KEY="your-key" python3 hooks/unbound.py
```

### Clear all logs

```bash
rm -f ~/.cursor/hooks/agent-audit.log
rm -f ~/.cursor/hooks/error.log
```

---

## Automated tests

Run the unit test suite (no Cursor or API key needed):

```bash
pip install pytest
python3 -m pytest tests/ -v
```

---

## Checklist

| # | Test | Status |
|---|---|---|
| A.1 | beforeShellExecution — safe command allowed | |
| A.2 | beforeShellExecution — blocked command denied | |
| A.3 | beforeShellExecution — fail open on API error | |
| B.1 | beforeSubmitPrompt — clean prompt allowed | |
| B.2 | beforeSubmitPrompt — DLP blocks PII | |
| C.1 | Audit — events logged to agent-audit.log | |
| D.1 | stop — exchange sent to dashboard | |
| D.2 | stop — offline fallback on failure | |
| E.1 | Install — single-command setup | |
| E.2 | Uninstall — clean removal | |
