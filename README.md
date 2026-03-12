# Unbound Cursor Plugin

Security, governance, and analytics for [Cursor](https://cursor.com) — powered by [Unbound AI](https://getunbound.ai).

## What it does

This plugin connects Cursor to the Unbound AI platform via hooks downloaded from the [websentry-ai/setup](https://github.com/websentry-ai/setup) public repo:

| Hook | What it enforces |
|---|---|
| **beforeShellExecution** | Policy check before shell commands |
| **beforeMCPExecution** | Policy check before MCP tool calls |
| **afterShellExecution** | Audit logging for shell commands |
| **afterMCPExecution** | Audit logging for MCP tool calls |
| **afterFileEdit** | Audit logging for file edits |
| **beforeReadFile** | Audit logging for file reads |
| **beforeSubmitPrompt** | Guardrails — DLP, NSFW, and jailbreak detection on user prompts |
| **afterAgentResponse** | Captures assistant responses for analytics |
| **stop** | Session analytics — sends the full conversation exchange |

All hooks **fail open**: if the API is unreachable or the key is missing, Cursor continues normally.

---

## Install

### From the Cursor Marketplace

Search for **Unbound** in the Cursor marketplace panel, or visit [cursor.com/marketplace](https://cursor.com/marketplace).

### From a local clone

```bash
git clone https://github.com/websentry-ai/cursor-extension.git
cd cursor-extension
./install.sh
```

This single command opens your browser for authentication, saves your API key, downloads hooks from the public repo, and restarts Cursor.

---

## Setup

### Via skill (recommended)

Run `/unbound-cursor:setup` in any Cursor conversation. The AI will walk you through the setup flow.

### Manual

Run in your terminal:

```bash
python3 scripts/setup.py --domain gateway.getunbound.ai
```

This will:
1. Open a browser for authentication
2. Save `UNBOUND_CURSOR_API_KEY` to your shell RC file
3. Download `hooks.json` to `~/.cursor/hooks.json`
4. Download `unbound.py` to `~/.cursor/hooks/unbound.py`
5. Restart Cursor

To update hooks only (without re-authenticating):

```bash
python3 scripts/setup.py --hooks-only
```

### Verify

After setup, test with:

- **Block policy**: create a BLOCK rule in your Unbound dashboard, then try running `rm -rf /` — should be blocked
- **DLP guardrail**: enable DLP, then type a prompt containing an SSN — should be blocked
- **Analytics**: run any command, then check your Unbound dashboard for the event

---

## Enterprise (MDM) install

For fleet deployment where users cannot disable the plugin, see [`enterprise/README.md`](enterprise/README.md).

Options:
1. **MDM**: Deploy `hooks.json` to the system-wide Cursor path + provision `UNBOUND_CURSOR_API_KEY` per device
2. **Team Marketplace**: Import the plugin repo in your Cursor Team Dashboard and mark as "required"

---

## Configuration

| Variable | Description |
|---|---|
| `UNBOUND_CURSOR_API_KEY` | Bearer token for the Unbound API. Get one at https://app.getunbound.ai > Settings > API Keys |

---

## Logs

| Path | Contents |
|---|---|
| `~/.cursor/hooks/agent-audit.log` | Per-session audit trail |
| `~/.cursor/hooks/error.log` | API errors (last 25 entries) |
| `~/.unbound/logs/debug.jsonl` | Raw stdin from every hook event (for debugging) |
| `~/.unbound/logs/offline-events.jsonl` | Exchanges that failed to send (replayed on reconnect) |

---

## Project structure

```
install.sh                 Single-command installer
uninstall.sh               Clean uninstaller
.cursor-plugin/
  plugin.json              Plugin manifest
  marketplace.json         Marketplace catalog
hooks/
  hooks.json               Plugin hook config (empty — hooks downloaded at setup)
rules/
  setup-guide.mdc          On-demand setup/reconfigure rule
skills/
  setup/SKILL.md           /unbound-cursor:setup skill
commands/
  setup.md                 Setup command
scripts/
  setup.py                 Browser OAuth + hook download script
enterprise/
  hooks.json.tmpl          MDM template for fleet enforcement
  README.md                Enterprise deployment guide
tests/
  test_setup.py            Setup script tests
  requirements.txt         pytest
```

---

## Development

```bash
# Run all tests
pip install pytest
python3 -m pytest tests/ -v

# Install locally for testing
./install.sh

# Uninstall
./uninstall.sh
```

---

## License

[MIT](LICENSE)
