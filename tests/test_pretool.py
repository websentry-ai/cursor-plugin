"""Unit tests for preToolUse hook — Unbound API integration (Cursor adapter).

Covers:
  - stdin → API payload transformation
  - API response → Cursor stdout transformation (via adapter)
  - Error paths (timeout, 500, missing env, malformed stdin)
  - handle_pre_tool_use() stdout output
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).parent.parent
_LIB = _ROOT / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from unbound import extract_command_for_pretool, process_pre_tool_use
from adapter import format_pretool_output

# Load hook-handler.py via importlib (filename contains a hyphen)
_spec = importlib.util.spec_from_file_location(
    "hook_handler", _ROOT / "scripts" / "hook-handler.py"
)
hh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hh)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_api_response(decision: str, reason: str = "") -> MagicMock:
    body = json.dumps({"decision": decision, "reason": reason}).encode()
    return MagicMock(returncode=0, stdout=body)


# ---------------------------------------------------------------------------
# extract_command_for_pretool — tool type mapping
# ---------------------------------------------------------------------------

class TestExtractCommandForPretool:

    def test_bash_uses_command_field(self):
        event = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}
        assert extract_command_for_pretool(event) == "rm -rf /"

    def test_write_uses_file_path(self):
        event = {"tool_name": "Write", "tool_input": {"file_path": "/etc/passwd"}}
        assert extract_command_for_pretool(event) == "/etc/passwd"

    def test_read_uses_file_path(self):
        event = {"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}}
        assert extract_command_for_pretool(event) == "/etc/hosts"

    def test_delete_uses_file_path(self):
        event = {"tool_name": "Delete", "tool_input": {"file_path": "/tmp/file"}}
        assert extract_command_for_pretool(event) == "/tmp/file"

    def test_grep_uses_pattern(self):
        event = {"tool_name": "Grep", "tool_input": {"pattern": "password"}}
        assert extract_command_for_pretool(event) == "password"

    def test_mcp_uses_tool_name_from_input(self):
        event = {"tool_name": "MCP", "tool_input": {"tool_name": "slack_send"}}
        assert extract_command_for_pretool(event) == "slack_send"

    def test_task_uses_prompt(self):
        event = {"tool_name": "Task", "tool_input": {"prompt": "do something"}}
        assert extract_command_for_pretool(event) == "do something"

    def test_unknown_tool_falls_back_to_tool_name(self):
        event = {"tool_name": "CustomTool", "tool_input": {"other": "value"}}
        assert extract_command_for_pretool(event) == "CustomTool"

    def test_empty_event_returns_empty_string(self):
        assert extract_command_for_pretool({}) == ""


# ---------------------------------------------------------------------------
# API payload construction
# ---------------------------------------------------------------------------

class TestApiPayload:

    @patch("subprocess.run")
    def test_payload_includes_conversation_id(self, mock_run):
        mock_run.return_value = _make_api_response("allow")
        process_pre_tool_use(
            {"session_id": "my-session", "tool_name": "Bash", "tool_input": {"command": "ls"}},
            "key",
        )
        cmd = mock_run.call_args[0][0]
        payload = json.loads(cmd[cmd.index("-d") + 1])
        assert payload["conversation_id"] == "my-session"

    @patch("subprocess.run")
    def test_payload_sets_cursor_app_label(self, mock_run):
        mock_run.return_value = _make_api_response("allow")
        process_pre_tool_use(
            {"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "pwd"}},
            "key",
        )
        cmd = mock_run.call_args[0][0]
        payload = json.loads(cmd[cmd.index("-d") + 1])
        assert payload["event_name"] == "tool_use"
        assert payload["unbound_app_label"] == "cursor"


# ---------------------------------------------------------------------------
# Adapter output formatting (preToolUse)
# ---------------------------------------------------------------------------

class TestPretoolOutputFormat:

    def test_deny_uses_cursor_format(self):
        result = format_pretool_output({"decision": "deny", "reason": "Blocked"})
        assert result == {"decision": "deny", "reason": "Blocked"}

    def test_allow_uses_cursor_format(self):
        result = format_pretool_output({"decision": "allow"})
        assert result == {"decision": "allow"}

    def test_ask_maps_to_deny(self):
        result = format_pretool_output({"decision": "ask", "reason": "Review"})
        assert result["decision"] == "deny"


# ---------------------------------------------------------------------------
# Handler stdout output
# ---------------------------------------------------------------------------

class TestPreToolUseHandlerOutput:

    PAYLOAD = {"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "ls"}}

    def test_no_api_key_prints_allow(self, capsys):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("UNBOUND_CURSOR_API_KEY", None)
            hh.handle_pre_tool_use(self.PAYLOAD)
        out = json.loads(capsys.readouterr().out)
        assert out["decision"] == "allow"

    def test_api_deny_prints_deny(self, capsys):
        with patch.object(hh, "_call_pretool_api", return_value={"decision": "deny", "reason": "Blocked"}), \
             patch.dict(os.environ, {"UNBOUND_CURSOR_API_KEY": "key"}):
            hh.handle_pre_tool_use(self.PAYLOAD)
        out = json.loads(capsys.readouterr().out)
        assert out["decision"] == "deny"
        assert out["reason"] == "Blocked"

    def test_api_allow_prints_allow(self, capsys):
        with patch.object(hh, "_call_pretool_api", return_value={"decision": "allow"}), \
             patch.dict(os.environ, {"UNBOUND_CURSOR_API_KEY": "key"}):
            hh.handle_pre_tool_use(self.PAYLOAD)
        out = json.loads(capsys.readouterr().out)
        assert out["decision"] == "allow"

    def test_api_exception_prints_allow(self, capsys):
        with patch.object(hh, "_call_pretool_api", side_effect=RuntimeError("boom")), \
             patch.dict(os.environ, {"UNBOUND_CURSOR_API_KEY": "key"}):
            hh.handle_pre_tool_use(self.PAYLOAD)
        out = json.loads(capsys.readouterr().out)
        assert out["decision"] == "allow"

    def test_empty_api_response_prints_allow(self, capsys):
        with patch.object(hh, "_call_pretool_api", return_value={}), \
             patch.dict(os.environ, {"UNBOUND_CURSOR_API_KEY": "key"}):
            hh.handle_pre_tool_use(self.PAYLOAD)
        out = json.loads(capsys.readouterr().out)
        assert out["decision"] == "allow"

    def test_output_is_valid_json(self, capsys):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("UNBOUND_CURSOR_API_KEY", None)
            hh.handle_pre_tool_use(self.PAYLOAD)
        raw = capsys.readouterr().out.strip()
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# Error paths — all must fail open
# ---------------------------------------------------------------------------

class TestErrorPaths:

    STDIN = {
        "session_id": "sess-abc",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "model": "gpt-4",
    }

    def test_empty_api_key_returns_empty(self):
        assert process_pre_tool_use(self.STDIN, "") == {}

    def test_none_api_key_returns_empty(self):
        assert process_pre_tool_use(self.STDIN, None) == {}

    @patch("subprocess.run")
    def test_api_500_returns_empty(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout=b"", stderr=b"500 Internal Server Error")
        assert process_pre_tool_use(self.STDIN, "key") == {}

    @patch("subprocess.run", side_effect=Exception("Connection timed out"))
    def test_network_timeout_returns_empty(self, mock_run):
        assert process_pre_tool_use(self.STDIN, "key") == {}

    @patch("subprocess.run")
    def test_malformed_json_returns_empty(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"not-valid-json{{{")
        assert process_pre_tool_use(self.STDIN, "key") == {}
