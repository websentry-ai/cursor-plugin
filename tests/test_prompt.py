"""Unit tests for beforeSubmitPrompt hook — Cursor adapter.

Covers:
  - stdin → API payload transformation
  - API response → Cursor stdout (continue:false or empty)
  - Error paths (timeout, 500, missing env, malformed stdin)
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

from adapter import format_prompt_output

# Load hook-handler.py
_spec = importlib.util.spec_from_file_location(
    "hook_handler", _ROOT / "scripts" / "hook-handler.py"
)
hh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hh)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _api_ok(decision: str, reason: str = "") -> MagicMock:
    body = json.dumps({"decision": decision, "reason": reason}).encode()
    return MagicMock(returncode=0, stdout=body)


def _api_fail() -> MagicMock:
    return MagicMock(returncode=1, stdout=b"", stderr=b"500 error")


# ---------------------------------------------------------------------------
# API payload
# ---------------------------------------------------------------------------

class TestPromptPayload:

    @patch("subprocess.run")
    def test_prompt_sent_as_user_message(self, mock_run):
        mock_run.return_value = _api_ok("allow")
        with patch.object(hh, "_audit_log"):
            with patch.dict("os.environ", {"UNBOUND_API_KEY": "key"}):
                hh.handle_before_submit_prompt(
                    {"session_id": "s1", "prompt": "hello world"}
                )
        cmd = mock_run.call_args[0][0]
        payload = json.loads(cmd[cmd.index("-d") + 1])
        assert payload["event_name"] == "user_prompt"
        assert payload["messages"][0] == {"role": "user", "content": "hello world"}

    @patch("subprocess.run")
    def test_uses_cursor_app_label(self, mock_run):
        mock_run.return_value = _api_ok("allow")
        with patch.object(hh, "_audit_log"):
            with patch.dict("os.environ", {"UNBOUND_API_KEY": "key"}):
                hh.handle_before_submit_prompt(
                    {"session_id": "my-session", "prompt": "test"}
                )
        cmd = mock_run.call_args[0][0]
        payload = json.loads(cmd[cmd.index("-d") + 1])
        assert payload["conversation_id"] == "my-session"
        assert payload["unbound_app_label"] == "cursor"


# ---------------------------------------------------------------------------
# Response formatting
# ---------------------------------------------------------------------------

class TestPromptResponse:

    @patch("subprocess.run")
    def test_deny_outputs_continue_false(self, mock_run, capsys):
        mock_run.return_value = _api_ok("deny", "PII detected")
        with patch.dict("os.environ", {"UNBOUND_API_KEY": "key"}):
            hh.handle_before_submit_prompt({"session_id": "s", "prompt": "my SSN is 123"})
        out = json.loads(capsys.readouterr().out)
        assert out["continue"] is False
        assert out["user_message"] == "PII detected"

    @patch("subprocess.run")
    def test_allow_produces_no_output(self, mock_run, capsys):
        mock_run.return_value = _api_ok("allow")
        with patch.object(hh, "_audit_log"):
            with patch.dict("os.environ", {"UNBOUND_API_KEY": "key"}):
                hh.handle_before_submit_prompt({"session_id": "s", "prompt": "clean prompt"})
        assert capsys.readouterr().out == ""

    @patch("subprocess.run")
    def test_blocked_prompt_is_not_logged(self, mock_run):
        mock_run.return_value = _api_ok("deny", "blocked")
        with patch.object(hh, "_audit_log") as mock_log:
            with patch.dict("os.environ", {"UNBOUND_API_KEY": "key"}):
                hh.handle_before_submit_prompt({"session_id": "s", "prompt": "bad"})
        mock_log.assert_not_called()

    @patch("subprocess.run")
    def test_allowed_prompt_is_logged(self, mock_run):
        mock_run.return_value = _api_ok("allow")
        with patch.object(hh, "_audit_log") as mock_log:
            with patch.dict("os.environ", {"UNBOUND_API_KEY": "key"}):
                hh.handle_before_submit_prompt({"session_id": "s", "prompt": "good"})
        mock_log.assert_called_once()
        log_entry = mock_log.call_args[0][0]
        assert log_entry["event"]["hook_event_name"] == "beforeSubmitPrompt"


# ---------------------------------------------------------------------------
# Error paths — all fail open
# ---------------------------------------------------------------------------

class TestPromptErrorPaths:

    def test_no_api_key_logs_and_no_output(self, capsys):
        with patch.object(hh, "_audit_log") as mock_log:
            with patch.dict("os.environ", {}, clear=True):
                os.environ.pop("UNBOUND_API_KEY", None)
                hh.handle_before_submit_prompt({"session_id": "s", "prompt": "hi"})
        assert capsys.readouterr().out == ""
        mock_log.assert_called_once()

    @patch("subprocess.run")
    def test_api_500_allows_and_logs(self, mock_run, capsys):
        mock_run.return_value = _api_fail()
        with patch.object(hh, "_audit_log") as mock_log:
            with patch.dict("os.environ", {"UNBOUND_API_KEY": "key"}):
                hh.handle_before_submit_prompt({"session_id": "s", "prompt": "hi"})
        assert capsys.readouterr().out == ""
        mock_log.assert_called_once()

    @patch("subprocess.run", side_effect=Exception("timeout"))
    def test_timeout_allows_and_logs(self, mock_run, capsys):
        with patch.object(hh, "_audit_log") as mock_log:
            with patch.dict("os.environ", {"UNBOUND_API_KEY": "key"}):
                hh.handle_before_submit_prompt({"session_id": "s", "prompt": "hi"})
        assert capsys.readouterr().out == ""
        mock_log.assert_called_once()


# ---------------------------------------------------------------------------
# Adapter transformer
# ---------------------------------------------------------------------------

class TestFormatPromptOutput:

    def test_deny_maps_to_continue_false(self):
        result = format_prompt_output({"decision": "deny", "reason": "PII"})
        assert result == {"continue": False, "user_message": "PII"}

    def test_allow_returns_empty(self):
        assert format_prompt_output({"decision": "allow"}) == {}

    def test_empty_returns_empty(self):
        assert format_prompt_output({}) == {}
