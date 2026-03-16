"""Unit tests for the Cursor ↔ Unbound schema adapter layer.

Covers:
  - normalize_input(): conversation_id → session_id injection, hook_event_name, tool name mapping
  - format_pretool_output(): allow, deny, ask→deny mapping
  - format_prompt_output(): deny→continue:false, allow→empty
  - format_fail_open(): per-hook defaults
  - map_tool_name(): Shell→Bash and pass-through
"""

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).parent.parent
_LIB = _ROOT / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from adapter import (
    format_fail_open,
    format_pretool_output,
    format_prompt_output,
    map_tool_name,
    normalize_input,
)


# ---------------------------------------------------------------------------
# normalize_input()
# ---------------------------------------------------------------------------

class TestNormalizeInput:

    def test_injects_session_id_from_conversation_id(self):
        payload = {"conversation_id": "conv-123", "model": "gpt-4"}
        result = normalize_input("preToolUse", payload)
        assert result["session_id"] == "conv-123"

    def test_preserves_conversation_id(self):
        payload = {"conversation_id": "conv-123"}
        result = normalize_input("preToolUse", payload)
        assert result["conversation_id"] == "conv-123"

    def test_injects_hook_event_name(self):
        result = normalize_input("beforeSubmitPrompt", {"conversation_id": "c"})
        assert result["hook_event_name"] == "beforeSubmitPrompt"

    def test_maps_tool_name_for_pretooluse(self):
        payload = {"conversation_id": "c", "tool_name": "Shell", "tool_input": {"command": "ls"}}
        result = normalize_input("preToolUse", payload)
        assert result["tool_name"] == "Bash"

    def test_does_not_map_tool_name_for_other_events(self):
        payload = {"conversation_id": "c", "tool_name": "Shell"}
        result = normalize_input("postToolUse", payload)
        assert result["tool_name"] == "Shell"

    def test_missing_conversation_id_defaults_to_empty(self):
        result = normalize_input("preToolUse", {})
        assert result["session_id"] == ""

    def test_preserves_all_original_fields(self):
        payload = {"conversation_id": "c", "model": "gpt-4", "extra": "data"}
        result = normalize_input("preToolUse", payload)
        assert result["model"] == "gpt-4"
        assert result["extra"] == "data"

    def test_does_not_mutate_original_payload(self):
        payload = {"conversation_id": "c", "tool_name": "Shell"}
        normalize_input("preToolUse", payload)
        assert payload["tool_name"] == "Shell"  # original unchanged
        assert "session_id" not in payload


# ---------------------------------------------------------------------------
# format_pretool_output()
# ---------------------------------------------------------------------------

class TestFormatPretoolOutput:

    def test_allow(self):
        result = format_pretool_output({"decision": "allow", "reason": ""})
        assert result == {"decision": "allow"}

    def test_deny_with_reason(self):
        result = format_pretool_output({"decision": "deny", "reason": "Blocked by policy"})
        assert result == {"decision": "deny", "reason": "Blocked by policy"}

    def test_ask_maps_to_deny(self):
        result = format_pretool_output({"decision": "ask", "reason": "Needs review"})
        assert result == {"decision": "deny", "reason": "Needs review"}

    def test_empty_response_returns_allow(self):
        assert format_pretool_output({}) == {"decision": "allow"}

    def test_none_response_returns_allow(self):
        assert format_pretool_output(None) == {"decision": "allow"}

    def test_missing_decision_defaults_to_allow(self):
        result = format_pretool_output({"reason": "some reason"})
        assert result["decision"] == "allow"

    def test_deny_without_reason_omits_reason_key(self):
        result = format_pretool_output({"decision": "deny"})
        assert result == {"decision": "deny"}


# ---------------------------------------------------------------------------
# format_prompt_output()
# ---------------------------------------------------------------------------

class TestFormatPromptOutput:

    def test_deny_maps_to_continue_false(self):
        result = format_prompt_output({"decision": "deny", "reason": "PII detected"})
        assert result == {"continue": False, "user_message": "PII detected"}

    def test_allow_returns_empty(self):
        assert format_prompt_output({"decision": "allow"}) == {}

    def test_empty_response_returns_empty(self):
        assert format_prompt_output({}) == {}

    def test_none_response_returns_empty(self):
        assert format_prompt_output(None) == {}

    def test_deny_without_reason_omits_user_message(self):
        result = format_prompt_output({"decision": "deny"})
        assert result == {"continue": False}

    def test_missing_decision_defaults_to_allow(self):
        assert format_prompt_output({"reason": "something"}) == {}


# ---------------------------------------------------------------------------
# format_fail_open()
# ---------------------------------------------------------------------------

class TestFormatFailOpen:

    def test_pretooluse_returns_allow(self):
        assert format_fail_open("preToolUse") == {"decision": "allow"}

    def test_beforesubmitprompt_returns_continue_true(self):
        assert format_fail_open("beforeSubmitPrompt") == {"continue": True}

    def test_posttooluse_returns_empty(self):
        assert format_fail_open("postToolUse") == {}

    def test_sessionend_returns_empty(self):
        assert format_fail_open("sessionEnd") == {}

    def test_unknown_event_returns_empty(self):
        assert format_fail_open("unknownEvent") == {}


# ---------------------------------------------------------------------------
# map_tool_name()
# ---------------------------------------------------------------------------

class TestMapToolName:

    def test_shell_maps_to_bash(self):
        assert map_tool_name("Shell") == "Bash"

    def test_read_passes_through(self):
        assert map_tool_name("Read") == "Read"

    def test_write_passes_through(self):
        assert map_tool_name("Write") == "Write"

    def test_grep_passes_through(self):
        assert map_tool_name("Grep") == "Grep"

    def test_delete_passes_through(self):
        assert map_tool_name("Delete") == "Delete"

    def test_mcp_passes_through(self):
        assert map_tool_name("MCP") == "MCP"

    def test_task_passes_through(self):
        assert map_tool_name("Task") == "Task"

    def test_unknown_tool_passes_through(self):
        assert map_tool_name("CustomTool") == "CustomTool"
