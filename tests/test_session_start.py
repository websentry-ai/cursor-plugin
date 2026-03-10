"""Unit tests for the sessionStart hook (scripts/session-start.py).

Covers:
  - Missing API key → outputs additional_context with setup instructions
  - Present API key → outputs short "active" note
  - is_background_agent: true → skips setup prompt
  - Exception handling → outputs {"continue": true} (fail open)
  - Output is valid JSON
"""

import importlib.util
import json
import os
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Load session-start.py via importlib (filename has a hyphen)
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "session_start", _ROOT / "scripts" / "session-start.py"
)
ss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ss)


def _run(stdin_data: str = "{}", env_overrides: dict = None):
    """Run main() with given stdin and env, return parsed JSON output."""
    env = env_overrides or {}
    with patch.object(sys, "stdin", StringIO(stdin_data)), \
         patch.dict(os.environ, env, clear=True), \
         pytest.raises(SystemExit) as exc:
        ss.main()
    return exc.value.code


def _capture(stdin_data: str = "{}", env_overrides: dict = None, capsys=None):
    """Run main() and return (exit_code, parsed_output)."""
    env = env_overrides or {}
    with patch.object(sys, "stdin", StringIO(stdin_data)), \
         patch.dict(os.environ, env, clear=True), \
         pytest.raises(SystemExit) as exc:
        ss.main()
    output = json.loads(capsys.readouterr().out)
    return exc.value.code, output


class TestSessionStartMissingKey:

    def test_outputs_additional_context_with_setup_instructions(self, capsys):
        code, out = _capture("{}", {}, capsys)
        assert code == 0
        assert "additional_context" in out
        assert "UNBOUND_CURSOR_API_KEY" in out["additional_context"]
        assert "setup" in out["additional_context"].lower()

    def test_continue_is_true(self, capsys):
        _, out = _capture("{}", {}, capsys)
        assert out["continue"] is True


class TestSessionStartWithKey:

    def test_outputs_active_status(self, capsys):
        code, out = _capture("{}", {"UNBOUND_CURSOR_API_KEY": "test-key-12345"}, capsys)
        assert code == 0
        assert "additional_context" in out
        assert "active" in out["additional_context"].lower()

    def test_continue_is_true(self, capsys):
        _, out = _capture("{}", {"UNBOUND_CURSOR_API_KEY": "key"}, capsys)
        assert out["continue"] is True

    def test_does_not_mention_setup(self, capsys):
        _, out = _capture("{}", {"UNBOUND_CURSOR_API_KEY": "key"}, capsys)
        assert "setup.py" not in out["additional_context"]


class TestSessionStartBackgroundAgent:

    def test_skips_setup_prompt(self, capsys):
        payload = json.dumps({"is_background_agent": True})
        code, out = _capture(payload, {}, capsys)
        assert code == 0
        assert out == {"continue": True}

    def test_skips_even_with_key(self, capsys):
        payload = json.dumps({"is_background_agent": True})
        _, out = _capture(payload, {"UNBOUND_CURSOR_API_KEY": "key"}, capsys)
        assert out == {"continue": True}


class TestSessionStartOutput:

    def test_output_is_valid_json(self, capsys):
        with patch.object(sys, "stdin", StringIO("{}")), \
             patch.dict(os.environ, {}, clear=True), \
             pytest.raises(SystemExit):
            ss.main()
        raw = capsys.readouterr().out.strip()
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)

    def test_always_exits_0(self):
        code = _run("{}", {})
        assert code == 0
