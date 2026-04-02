# Unbound for Cursor

Security, compliance, and observability for [Cursor](https://cursor.com) — powered by [Unbound](https://getunbound.ai).

## Overview

Unbound monitors every AI-driven action in Cursor in real time — shell commands, MCP tool calls, file reads and edits, user prompts, and assistant responses. It enforces the policies and guardrails you configure in the [Unbound dashboard](https://app.getunbound.ai), and streams full session data for analytics and audit.

**Key capabilities:**

- **Command policy** — block or allow shell and MCP tool executions based on custom rules
- **Prompt guardrails** — DLP (PII detection), NSFW filtering, and jailbreak detection on user prompts
- **Audit trail** — every AI action is logged locally and sent to your Unbound dashboard
- **Session analytics** — full conversation exchanges streamed on session end
- **Fail-open design** — if the API is unreachable or the key is missing, Cursor continues normally

## Quick start

### 1. Install the plugin

**From Cursor Marketplace**

Search for **Unbound** in Settings > Plugins, or visit [cursor.com/marketplace](https://cursor.com/marketplace).

**From source**

```bash
git clone https://github.com/websentry-ai/cursor-extension.git
cd cursor-extension
./install.sh
```

### 2. Authenticate

The installer opens your browser automatically. Sign in to your Unbound account and the API key is saved to your shell profile.

Or run the setup skill inside Cursor:

```
/unbound-cursor:setup
```

### 3. Verify

After setup, try these in Cursor:

1. Ask the AI to run `ls` — check your [Unbound dashboard](https://app.getunbound.ai) for the event
2. Create a BLOCK rule in the dashboard, then ask the AI to run that command — it should be blocked
3. Enable DLP guardrails, then type a prompt containing a fake SSN — it should be blocked

## What gets monitored

| Event | When it fires | What Unbound does |
|---|---|---|
| Shell command | Before execution | Checks against your command policies |
| MCP tool call | Before execution | Checks against your tool policies |
| User prompt | Before submission | Runs DLP, NSFW, and jailbreak guardrails |
| File read | Before read | Logs to audit trail |
| File edit | After edit | Logs to audit trail |
| Shell output | After execution | Logs to audit trail |
| MCP result | After execution | Logs to audit trail |
| Agent response | After response | Captures for session analytics |
| Session end | On stop | Sends full exchange to dashboard |

All checks happen in real time. Blocked actions show an explanation to the user.

## Configuration

The only required configuration is your API key:

| Variable | Description |
|---|---|
| `UNBOUND_CURSOR_API_KEY` | Your Unbound API key. Get one at [app.getunbound.ai](https://app.getunbound.ai) > Settings > API Keys |

The installer and `/unbound-cursor:setup` skill handle this automatically. The key is saved to your shell profile (`~/.zprofile`, `~/.bashrc`, etc.) and picked up by Cursor on launch.

## Enterprise deployment

For fleet-wide enforcement where users cannot disable the plugin:

- **MDM** — deploy hooks to the system-wide Cursor path and provision API keys per device
- **Team Marketplace** — import this repo in your Cursor Team Dashboard and mark as "required"

See [`enterprise/README.md`](enterprise/README.md) for detailed instructions.

## Uninstall

```bash
./uninstall.sh
```

Or remove the plugin from Cursor's Settings > Plugins panel.

## Support

- Dashboard: [app.getunbound.ai](https://app.getunbound.ai)
- Documentation: [docs.getunbound.ai](https://docs.getunbound.ai)
- Issues: [github.com/websentry-ai/cursor-extension/issues](https://github.com/websentry-ai/cursor-extension/issues)

## License

[MIT](LICENSE)
