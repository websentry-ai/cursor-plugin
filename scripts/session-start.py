#!/usr/bin/env python3
"""Cursor sessionStart hook — detects missing UNBOUND_API_KEY and injects setup context.

When the API key is missing, injects setup instructions into the AI's context
via `additional_context` so the AI proactively guides the user through setup.

When the API key is present, injects a short status note confirming the plugin
is active.

This script must always fail open — any exception outputs {"continue": true}.
"""

import json
import os
import sys


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

    api_key = os.getenv("UNBOUND_API_KEY")

    if not api_key:
        output = {
            "additional_context": (
                "UNBOUND_API_KEY is not configured. The Unbound security plugin "
                "cannot enforce policies without it.\n\n"
                "To set up, ask the user to run this command in their terminal:\n"
                "  python3 scripts/setup.py --domain gateway.getunbound.ai\n\n"
                "After setup completes, the user must restart Cursor for the key "
                "to take effect.\n\n"
                "Proactively inform the user that Unbound setup is needed and offer "
                "to guide them through the process."
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
