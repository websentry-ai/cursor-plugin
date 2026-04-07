# AGENTS.md

## Cursor Cloud specific instructions

### Overview

This is a **Cursor IDE plugin** (not a web app). The main components are:

- `hooks/unbound.py` — Hook event processor invoked by Cursor on lifecycle events (reads JSON from stdin, outputs JSON to stdout)
- `scripts/setup.py` — OAuth setup script (browser-based, requires `--domain` flag)
- `hooks/hooks.json` — Hook registration manifest
- `.cursor-plugin/plugin.json` — Plugin metadata

### Dependencies

- **Python 3.8+** (stdlib only; no pip packages needed for the plugin itself)
- **curl** (used by `unbound.py` for HTTP calls via subprocess)
- **pytest** (for running tests: `pip install -r tests/requirements.txt`)

### Running tests

```bash
python3 -m pytest tests/ -v
```

No API key or Cursor IDE is needed for the unit tests.

### Linting

No lint config is committed. To lint:

```bash
flake8 hooks/unbound.py scripts/setup.py tests/test_setup.py --max-line-length=120
```

Pre-existing style warnings exist; the codebase does not enforce zero-warning linting.

### Manual hook testing

Test `unbound.py` by piping JSON events to stdin (no API key needed for fail-open mode):

```bash
echo '{"hook_event_name":"beforeShellExecution","conversation_id":"test-123","command":"ls","model":"gpt-4","generation_id":"gen-1"}' | python3 hooks/unbound.py
```

Expected output: `{}` (empty JSON = allow / no-op).

### Key caveats

- The `install.sh` and `scripts/setup.py --domain ...` commands open a browser for OAuth and wait for a callback. They are interactive and should **not** be run in headless Cloud Agent sessions.
- `scripts/setup.py --clear` is safe to run headlessly (removes the API key from the shell RC file).
- Audit logs are written to `~/.cursor/hooks/agent-audit.log` and `~/.cursor/hooks/error.log`.
- The plugin operates in **fail-open** mode when `UNBOUND_CURSOR_API_KEY` is unset or invalid (all hooks return `{}` and Cursor continues normally).
