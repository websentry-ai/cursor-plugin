"""Sanity tests — production-critical behaviour across all handlers.

Covers:
  P0  _make_log_entry()    — log structure correctness
  P0  write_debug_log()    — debug file I/O
  P0  _write_offline()     — offline fallback file I/O
  P1  main() dispatch      — event routing + edge-case stdin
"""

import importlib.util
import json
import os
import re
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module setup
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
# P0 — _make_log_entry() structure
# ===========================================================================

class TestMakeLogEntry:

    def test_adds_hook_event_name_to_event(self):
        entry = hh._make_log_entry("beforeSubmitPrompt", {"prompt": "hi"})
        assert entry["event"]["hook_event_name"] == "beforeSubmitPrompt"

    def test_preserves_all_payload_fields(self):
        payload = {"session_id": "s1", "prompt": "test", "model": "gpt-4"}
        entry = hh._make_log_entry("beforeSubmitPrompt", payload)
        assert entry["event"]["prompt"] == "test"
        assert entry["event"]["model"] == "gpt-4"
        assert entry["event"]["session_id"] == "s1"

    def test_extracts_session_id_to_top_level(self):
        entry = hh._make_log_entry("sessionEnd", {"session_id": "abc-123"})
        assert entry["session_id"] == "abc-123"

    def test_missing_session_id_sets_none(self):
        entry = hh._make_log_entry("sessionEnd", {})
        assert entry["session_id"] is None

    def test_timestamp_is_iso8601_with_trailing_z(self):
        entry = hh._make_log_entry("sessionEnd", {})
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", entry["timestamp"])
        assert entry["timestamp"].endswith("Z")

    def test_works_for_all_cursor_event_types(self):
        for event in ("preToolUse", "beforeSubmitPrompt", "postToolUse", "sessionEnd"):
            entry = hh._make_log_entry(event, {"session_id": "s"})
            assert entry["event"]["hook_event_name"] == event

    def test_does_not_mutate_original_payload(self):
        payload = {"session_id": "s", "prompt": "hi"}
        hh._make_log_entry("beforeSubmitPrompt", payload)
        assert "hook_event_name" not in payload


# ===========================================================================
# P0 — write_debug_log()
# ===========================================================================

class TestWriteDebugLog:

    def test_creates_debug_log_file(self, tmp_path):
        with patch.object(hh, "LOG_DIR", tmp_path), \
             patch.object(hh, "DEBUG_LOG", tmp_path / "debug.jsonl"):
            hh.write_debug_log("preToolUse", {"tool_name": "Shell"})
        assert (tmp_path / "debug.jsonl").exists()

    def test_entry_contains_ts_event_stdin(self, tmp_path):
        debug_log = tmp_path / "debug.jsonl"
        with patch.object(hh, "LOG_DIR", tmp_path), \
             patch.object(hh, "DEBUG_LOG", debug_log):
            hh.write_debug_log("postToolUse", {"key": "val"})
        entry = json.loads(debug_log.read_text().strip())
        assert "ts" in entry
        assert entry["event"] == "postToolUse"
        assert entry["stdin"] == {"key": "val"}

    def test_appends_multiple_entries(self, tmp_path):
        debug_log = tmp_path / "debug.jsonl"
        with patch.object(hh, "LOG_DIR", tmp_path), \
             patch.object(hh, "DEBUG_LOG", debug_log):
            hh.write_debug_log("preToolUse", {"n": 1})
            hh.write_debug_log("sessionEnd", {"n": 2})
        lines = debug_log.read_text().strip().split("\n")
        assert len(lines) == 2


# ===========================================================================
# P0 — _write_offline()
# ===========================================================================

class TestWriteOffline:

    EXCHANGE = {"conversation_id": "s1", "messages": [], "model": "auto"}

    def test_creates_offline_events_file(self, tmp_path):
        offline = tmp_path / "offline-events.jsonl"
        with patch.object(hh, "LOG_DIR", tmp_path), \
             patch.object(hh, "OFFLINE_LOG", offline):
            hh._write_offline(self.EXCHANGE)
        assert offline.exists()

    def test_entry_preserves_exchange_data(self, tmp_path):
        offline = tmp_path / "offline-events.jsonl"
        with patch.object(hh, "LOG_DIR", tmp_path), \
             patch.object(hh, "OFFLINE_LOG", offline):
            hh._write_offline(self.EXCHANGE)
        entry = json.loads(offline.read_text().strip())
        assert entry["exchange"]["conversation_id"] == "s1"

    def test_entry_has_ts_field(self, tmp_path):
        offline = tmp_path / "offline-events.jsonl"
        with patch.object(hh, "LOG_DIR", tmp_path), \
             patch.object(hh, "OFFLINE_LOG", offline):
            hh._write_offline(self.EXCHANGE)
        entry = json.loads(offline.read_text().strip())
        assert "ts" in entry


# ===========================================================================
# P1 — main() dispatch
# ===========================================================================

class TestMainDispatch:

    def _run(self, argv, stdin_data="{}"):
        with patch.object(sys, "argv", argv), \
             patch.object(sys, "stdin", StringIO(stdin_data)), \
             patch.object(hh, "write_debug_log"), \
             pytest.raises(SystemExit) as exc:
            hh.main()
        return exc.value.code

    def test_exits_with_code_0(self):
        m = MagicMock()
        with patch.dict(hh.HANDLERS, {"preToolUse": m}):
            assert self._run(["hook-handler.py", "preToolUse"]) == 0

    def test_routes_pretooluse(self):
        m = MagicMock()
        with patch.dict(hh.HANDLERS, {"preToolUse": m}):
            self._run(["hook-handler.py", "preToolUse"], '{"tool_name":"Shell"}')
        m.assert_called_once()

    def test_routes_beforesubmitprompt(self):
        m = MagicMock()
        with patch.dict(hh.HANDLERS, {"beforeSubmitPrompt": m}):
            self._run(["hook-handler.py", "beforeSubmitPrompt"], '{"prompt":"hi"}')
        m.assert_called_once()

    def test_routes_posttooluse(self):
        m = MagicMock()
        with patch.dict(hh.HANDLERS, {"postToolUse": m}):
            self._run(["hook-handler.py", "postToolUse"], '{"tool_name":"Shell"}')
        m.assert_called_once()

    def test_routes_sessionend(self):
        m = MagicMock()
        with patch.dict(hh.HANDLERS, {"sessionEnd": m}):
            self._run(["hook-handler.py", "sessionEnd"], '{"session_id":"s"}')
        m.assert_called_once()

    def test_unknown_event_calls_no_handler(self):
        called = []
        sentinel = MagicMock(side_effect=lambda p: called.append(p))
        with patch.dict(hh.HANDLERS, {"preToolUse": sentinel, "sessionEnd": sentinel}):
            self._run(["hook-handler.py", "UnknownEvent"])
        assert called == []

    def test_empty_stdin_passes_empty_dict(self):
        m = MagicMock()
        with patch.dict(hh.HANDLERS, {"sessionEnd": m}):
            self._run(["hook-handler.py", "sessionEnd"], "")
        # normalize_input adds session_id and hook_event_name
        call_payload = m.call_args[0][0]
        assert call_payload["hook_event_name"] == "sessionEnd"

    def test_malformed_json_passes_raw_dict(self):
        m = MagicMock()
        with patch.dict(hh.HANDLERS, {"sessionEnd": m}):
            self._run(["hook-handler.py", "sessionEnd"], "{{bad json}}")
        assert "raw" in m.call_args[0][0]
