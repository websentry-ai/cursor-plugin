"""Cursor ↔ Unbound schema translation layer.

Translates between Cursor's hook input/output schemas and the format
expected by unbound.py (which was originally written for Claude Code).

Key differences handled:
  - Cursor sends `conversation_id`; unbound.py expects `session_id` internally.
  - Cursor preToolUse output: {"decision": "allow|deny", "reason": "..."}
  - Cursor beforeSubmitPrompt output: {"continue": true|false, "user_message": "..."}
  - Cursor tool names differ slightly (Shell vs Bash, Delete vs Edit).
"""


# ---------------------------------------------------------------------------
# Input normalization — Cursor payload → unbound.py-compatible dict
# ---------------------------------------------------------------------------

def normalize_input(cursor_event: str, payload: dict) -> dict:
    """Normalize a Cursor hook payload so unbound.py functions can consume it.

    Cursor sends `conversation_id`; unbound.py uses `session_id` internally
    (for audit logs and exchange building). The Unbound API expects
    `conversation_id`, which is what Cursor already provides — so at the API
    boundary no translation is needed.

    Args:
        cursor_event: The hook event name from sys.argv (e.g. "preToolUse").
        payload: The raw JSON dict read from stdin.

    Returns:
        A dict with `session_id` injected and `hook_event_name` set.
    """
    normalized = dict(payload)

    # Inject session_id from conversation_id so unbound.py works unmodified
    normalized["session_id"] = payload.get("conversation_id", "")

    # Inject hook_event_name so downstream code can inspect it
    normalized["hook_event_name"] = cursor_event

    # Map Cursor tool names to Claude Code equivalents for extract_command_for_pretool()
    if cursor_event == "preToolUse" and "tool_name" in normalized:
        normalized["tool_name"] = map_tool_name(normalized["tool_name"])

    return normalized


# ---------------------------------------------------------------------------
# Output formatting — Unbound API response → Cursor-expected JSON
# ---------------------------------------------------------------------------

def format_pretool_output(unbound_response: dict) -> dict:
    """Format an Unbound API response for Cursor's preToolUse hook.

    Unbound API returns: {"decision": "allow|deny|ask", "reason": "..."}
    Cursor expects:      {"decision": "allow|deny", "reason": "..."}

    Cursor has no "ask" concept — map it to "deny" so the action is blocked
    and the user sees the reason.
    """
    if not unbound_response:
        return {"decision": "allow"}

    decision = unbound_response.get("decision", "allow")
    reason = unbound_response.get("reason", "")

    # Cursor doesn't support "ask" — treat as deny
    if decision == "ask":
        decision = "deny"

    result = {"decision": decision}
    if reason:
        result["reason"] = reason
    return result


def format_prompt_output(unbound_response: dict) -> dict:
    """Format an Unbound API response for Cursor's beforeSubmitPrompt hook.

    Unbound API returns: {"decision": "deny", "reason": "..."}  (for blocks)
    Cursor expects:      {"continue": false, "user_message": "..."}

    For allow decisions, return empty dict (no output needed).
    """
    if not unbound_response:
        return {}

    decision = unbound_response.get("decision", "allow")
    reason = unbound_response.get("reason", "")

    if decision == "deny":
        result = {"continue": False}
        if reason:
            result["user_message"] = reason
        return result

    return {}


def format_fail_open(cursor_event: str) -> dict:
    """Return the appropriate fail-open output for a given hook event.

    Used when there's no API key, an API error, or any unexpected exception.
    """
    if cursor_event == "preToolUse":
        return {"decision": "allow"}
    if cursor_event == "beforeSubmitPrompt":
        return {"continue": True}
    # postToolUse, sessionEnd — no output needed
    return {}


# ---------------------------------------------------------------------------
# Tool name mapping — Cursor tool names → Claude Code / Unbound equivalents
# ---------------------------------------------------------------------------

# Cursor tool types (from docs): Shell, Read, Write, Grep, Delete, MCP, Task
# Claude Code tool types: Bash, Read, Write, Edit, Grep, Glob, WebFetch, WebSearch, Task

_TOOL_NAME_MAP = {
    "Shell": "Bash",
    # Read, Write, Grep, Task are the same in both
}


def map_tool_name(cursor_tool_name: str) -> str:
    """Map a Cursor tool name to its Claude Code / Unbound equivalent.

    Most names are the same. The key difference is Shell → Bash.
    Unknown tool names pass through unchanged.
    """
    return _TOOL_NAME_MAP.get(cursor_tool_name, cursor_tool_name)
