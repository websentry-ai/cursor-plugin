"""Unit tests for postToolUse and sessionEnd hooks.

Covers:
  - postToolUse appends to audit log
  - sessionEnd builds exchange, sends to API, cleans up, offline fallback
  - Session event filtering
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

_spec = importlib.util.spec_from_file_location(
    "hook_handler", _ROOT / "scripts" / "hook-handler.py"
)
hh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hh)


# ===========================================================================
# postToolUse
# ===========================================================================

class TestPostToolUse:

    def test_appends_to_audit_log(self):
        payload = {
            "session_id": "s1",
            "tool_name": "Shell",
            "tool_input": {"command": "ls"},
            "tool_output": "file.txt",
            "duration": 1234,
        }
        with patch.object(hh, "_audit_log") as mock_log:
            hh.handle_post_tool_use(payload)
        mock_log.assert_called_once()
        entry = mock_log.call_args[0][0]
        assert entry["session_id"] == "s1"
        assert entry["event"]["hook_event_name"] == "postToolUse"
        assert entry["event"]["tool_name"] == "Shell"

    def test_includes_cursor_extra_fields(self):
        payload = {
            "session_id": "s1",
            "tool_name": "Shell",
            "tool_input": {"command": "ls"},
            "tool_output": "file.txt",
            "duration": 5432,
        }
        with patch.object(hh, "_audit_log") as mock_log:
            hh.handle_post_tool_use(payload)
        entry = mock_log.call_args[0][0]
        assert entry["event"]["duration"] == 5432
        assert entry["event"]["tool_output"] == "file.txt"

    def test_produces_no_output(self, capsys):
        with patch.object(hh, "_audit_log"):
            hh.handle_post_tool_use({"session_id": "s", "tool_name": "Shell"})
        assert capsys.readouterr().out == ""


# ===========================================================================
# sessionEnd
# ===========================================================================

class TestSessionEnd:

    _PAYLOAD = {"session_id": "sess-xyz", "transcript_path": "undefined"}

    def test_logs_event_to_audit_log(self):
        with patch.object(hh, "_audit_log") as mock_log, \
             patch.object(hh, "_load_logs", return_value=[]), \
             patch.dict("os.environ", {}, clear=True):
            os.environ.pop("UNBOUND_CURSOR_API_KEY", None)
            hh.handle_session_end(self._PAYLOAD)
        mock_log.assert_called_once()
        entry = mock_log.call_args[0][0]
        assert entry["event"]["hook_event_name"] == "sessionEnd"

    def test_no_api_key_skips_exchange(self):
        with patch.object(hh, "_audit_log"), \
             patch.object(hh, "_send_exchange") as mock_send, \
             patch.dict("os.environ", {}, clear=True):
            os.environ.pop("UNBOUND_CURSOR_API_KEY", None)
            hh.handle_session_end(self._PAYLOAD)
        mock_send.assert_not_called()

    def test_successful_send_cleans_up_session_logs(self):
        session_log = {
            "session_id": "sess-xyz",
            "timestamp": "2026-01-01T00:00:00Z",
            "event": {"hook_event_name": "beforeSubmitPrompt", "session_id": "sess-xyz", "prompt": "hi"},
        }
        other_log = {
            "session_id": "other-session",
            "timestamp": "2026-01-01T00:00:01Z",
            "event": {"hook_event_name": "beforeSubmitPrompt", "session_id": "other-session", "prompt": "yo"},
        }
        exchange = {"conversation_id": "sess-xyz", "messages": [{"role": "user", "content": "hi"}], "model": "auto", "permission_mode": "default"}

        with patch.object(hh, "_audit_log"), \
             patch.object(hh, "_load_logs", return_value=[session_log, other_log]), \
             patch.object(hh, "_build_exchange", return_value=exchange), \
             patch.object(hh, "_send_exchange", return_value=True) as mock_send, \
             patch.object(hh, "_save_logs") as mock_save, \
             patch.object(hh, "_cleanup_logs"), \
             patch.dict("os.environ", {"UNBOUND_CURSOR_API_KEY": "key"}):
            hh.handle_session_end(self._PAYLOAD)

        mock_send.assert_called_once_with(exchange, "key")
        saved = mock_save.call_args[0][0]
        assert len(saved) == 1
        assert saved[0]["session_id"] == "other-session"

    def test_failed_send_writes_offline_log(self, tmp_path):
        exchange = {"conversation_id": "sess-xyz", "messages": [], "model": "auto", "permission_mode": "default"}

        offline_file = tmp_path / "offline-events.jsonl"
        with patch.object(hh, "_audit_log"), \
             patch.object(hh, "_load_logs", return_value=[]), \
             patch.object(hh, "_build_exchange", return_value=exchange), \
             patch.object(hh, "_send_exchange", return_value=False), \
             patch.object(hh, "_save_logs"), \
             patch.object(hh, "_cleanup_logs"), \
             patch.object(hh, "OFFLINE_LOG", offline_file), \
             patch.object(hh, "LOG_DIR", tmp_path), \
             patch.dict("os.environ", {"UNBOUND_CURSOR_API_KEY": "key"}):
            hh.handle_session_end(self._PAYLOAD)

        assert offline_file.exists()
        entry = json.loads(offline_file.read_text().strip())
        assert entry["exchange"]["conversation_id"] == "sess-xyz"

    def test_no_exchange_built_skips_send(self):
        with patch.object(hh, "_audit_log"), \
             patch.object(hh, "_load_logs", return_value=[]), \
             patch.object(hh, "_build_exchange", return_value=None), \
             patch.object(hh, "_send_exchange") as mock_send, \
             patch.object(hh, "_cleanup_logs"), \
             patch.dict("os.environ", {"UNBOUND_CURSOR_API_KEY": "key"}):
            hh.handle_session_end(self._PAYLOAD)
        mock_send.assert_not_called()

    def test_exception_does_not_propagate(self):
        with patch.object(hh, "_audit_log"), \
             patch.object(hh, "_load_logs", side_effect=RuntimeError("disk full")), \
             patch.dict("os.environ", {"UNBOUND_CURSOR_API_KEY": "key"}):
            hh.handle_session_end(self._PAYLOAD)  # should not raise


# ===========================================================================
# Session event filtering
# ===========================================================================

class TestSessionEndFiltering:

    _PAYLOAD = {"session_id": "s1", "transcript_path": "undefined"}

    def _log(self, session_id, event_name, extra=None):
        event = {"hook_event_name": event_name, "session_id": session_id}
        if extra:
            event.update(extra)
        return {"session_id": session_id, "timestamp": "2026-01-01T00:00:00Z", "event": event}

    def test_only_includes_logs_after_prompt_submit(self):
        logs = [
            self._log("s1", "postToolUse"),                                    # before prompt — excluded
            self._log("s1", "beforeSubmitPrompt", {"prompt": "hi"}),           # anchor
            self._log("s1", "postToolUse"),                                    # after prompt — included
        ]
        with patch.object(hh, "_audit_log"), \
             patch.object(hh, "_load_logs", return_value=logs), \
             patch.object(hh, "_build_exchange") as mock_build, \
             patch.object(hh, "_send_exchange", return_value=True), \
             patch.object(hh, "_save_logs"), \
             patch.object(hh, "_cleanup_logs"), \
             patch.dict(os.environ, {"UNBOUND_CURSOR_API_KEY": "key"}):
            hh.handle_session_end(self._PAYLOAD)
        events = mock_build.call_args[0][0]
        assert len(events) == 2
        assert events[0]["event"]["hook_event_name"] == "beforeSubmitPrompt"
        assert events[1]["event"]["hook_event_name"] == "postToolUse"

    def test_filters_out_other_session_logs(self):
        logs = [
            self._log("s1", "beforeSubmitPrompt", {"prompt": "s1_msg"}),
            self._log("s2", "beforeSubmitPrompt", {"prompt": "s2_msg"}),
        ]
        with patch.object(hh, "_audit_log"), \
             patch.object(hh, "_load_logs", return_value=logs), \
             patch.object(hh, "_build_exchange") as mock_build, \
             patch.object(hh, "_send_exchange", return_value=True), \
             patch.object(hh, "_save_logs"), \
             patch.object(hh, "_cleanup_logs"), \
             patch.dict(os.environ, {"UNBOUND_CURSOR_API_KEY": "key"}):
            hh.handle_session_end(self._PAYLOAD)
        events = mock_build.call_args[0][0]
        assert all(e["session_id"] == "s1" for e in events)

    def test_no_prompt_submit_means_no_send(self):
        logs = [self._log("s1", "postToolUse")]
        with patch.object(hh, "_audit_log"), \
             patch.object(hh, "_load_logs", return_value=logs), \
             patch.object(hh, "_send_exchange") as mock_send, \
             patch.object(hh, "_cleanup_logs"), \
             patch.dict(os.environ, {"UNBOUND_CURSOR_API_KEY": "key"}):
            hh.handle_session_end(self._PAYLOAD)
        mock_send.assert_not_called()
