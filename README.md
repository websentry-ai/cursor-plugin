# Unbound Cursor Plugin

Security, governance, and analytics for [Cursor](https://cursor.com) — powered by [Unbound AI](https://getunbound.ai).

## What it does

This plugin connects Cursor to the Unbound AI platform:

| Hook | What it enforces |
|---|---|
| **sessionStart** | Auto-detects missing API key and prompts setup |
| **preToolUse** | Command policy — block or warn on dangerous tool invocations |
| **beforeSubmitPrompt** | Guardrails — DLP, NSFW, and jailbreak detection on user prompts |
| **postToolUse** | Audit logging — streams tool usage to the Unbound dashboard |
| **sessionEnd** | Session analytics — sends the full conversation exchange on session end |

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

This single command installs hooks, opens your browser for authentication, and saves your API key. Restart Cursor after.

---

## Setup

### Automatic (recommended)

Start a new conversation in Cursor. If `UNBOUND_CURSOR_API_KEY` is not set, the AI will automatically detect it and run the setup script for you — browser opens, you authenticate, done.

### Manual

Run in your terminal:

```bash
python3 scripts/setup.py --domain gateway.getunbound.ai
```

Then restart Cursor to pick up the new environment variable.

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
| `~/.cursor/hooks/agent-audit.log` | Per-session audit trail (beforeSubmitPrompt, postToolUse) |
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
  hooks.json               Hook event configuration (5 hooks)
rules/
  setup-guide.mdc          On-demand setup/reconfigure rule
scripts/
  hook-handler.py          Central hook dispatcher
  session-start.py         sessionStart — API key detection + setup prompt
  setup.py                 Browser OAuth setup script
  lib/
    adapter.py             Cursor ↔ Unbound schema translation
    unbound.py             Unbound API helpers
enterprise/
  hooks.json.tmpl          MDM template for fleet enforcement
  README.md                Enterprise deployment guide
tests/
  test_adapter.py          Adapter unit tests
  test_session_start.py    sessionStart hook tests
  test_pretool.py          preToolUse hook tests
  test_prompt.py           beforeSubmitPrompt tests
  test_session.py          postToolUse + sessionEnd tests
  test_sanity.py           Production readiness tests
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
