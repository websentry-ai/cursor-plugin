#!/usr/bin/env python3
"""Cursor sessionStart hook — detects missing UNBOUND_CURSOR_API_KEY and injects setup context.

When the API key is missing, injects setup instructions into the AI's context
via `additional_context` so the AI proactively guides the user through setup.

When the API key is present, injects a short status note confirming the plugin
is active.

This script must always fail open — any exception outputs {"continue": true}.
"""

import json
import os
import sys
from pathlib import Path

# Resolve absolute path to setup.py based on this script's location
_SCRIPTS_DIR = Path(__file__).resolve().parent
_SETUP_PY = _SCRIPTS_DIR / "setup.py"


def main() -> None:
    # Read stdin (sessionStart payload)
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    # Background agents should not prompt for setup
    if payload.get("is_background_agent"):
        print(json.dumps({"continue": True}))
        sys.exit(0)

    api_key = os.getenv("UNBOUND_CURSOR_API_KEY")

    if not api_key:
        setup_cmd = f"python3 {_SETUP_PY} --domain gateway.getunbound.ai"
        output = {
            "additional_context": (
                "UNBOUND_CURSOR_API_KEY is not configured. The Unbound security plugin "
                "cannot enforce policies without it.\n\n"
                "To set up, run this exact command:\n"
                f"  {setup_cmd}\n\n"
                "This opens a browser for authentication and saves the key to the "
                "user's shell RC file. After setup completes, the user must restart "
                "Cursor for the key to take effect.\n\n"
                "Proactively inform the user that Unbound setup is needed and offer "
                "to run the setup command for them."
            ),
            "continue": True,
        }
    else:
        output = {
            "additional_context": (
                "Unbound security plugin is active. Command policies, DLP guardrails, "
                "and session analytics are being enforced."
            ),
            "continue": True,
        }

    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Must never block the session — always fail open
        print(json.dumps({"continue": True}))
        sys.exit(0)
