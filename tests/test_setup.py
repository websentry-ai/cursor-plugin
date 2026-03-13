"""Unit tests for setup.py — env var management, URL normalization, and OAuth flow.

Covers:
  - clear_setup(): removes env var
  - set_env_var / remove_env_var: cross-platform env var persistence
  - normalize_url(): URL formatting
  - get_shell_rc_file(): shell detection
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Load setup.py via importlib (it's in scripts/)
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "setup", _ROOT / "scripts" / "setup.py"
)
setup_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup_mod)


# ---------------------------------------------------------------------------
# clear_setup()
# ---------------------------------------------------------------------------

class TestClearSetup:

    @patch.object(setup_mod, "remove_env_var", return_value=(True, "Removed"))
    def test_removes_env_var(self, mock_rm):
        setup_mod.clear_setup()
        mock_rm.assert_called_once_with("UNBOUND_CURSOR_API_KEY")

    @patch.object(setup_mod, "remove_env_var", return_value=(False, "Failed"))
    def test_handles_removal_failure(self, mock_rm):
        # Should not raise even on failure
        setup_mod.clear_setup()
        mock_rm.assert_called_once_with("UNBOUND_CURSOR_API_KEY")


# ---------------------------------------------------------------------------
# normalize_url()
# ---------------------------------------------------------------------------

class TestNormalizeUrl:

    def test_adds_https(self):
        assert setup_mod.normalize_url("gateway.example.com") == "https://gateway.example.com"

    def test_preserves_existing_https(self):
        assert setup_mod.normalize_url("https://gateway.example.com") == "https://gateway.example.com"

    def test_preserves_existing_http(self):
        assert setup_mod.normalize_url("http://localhost:8080") == "http://localhost:8080"

    def test_strips_trailing_slash(self):
        assert setup_mod.normalize_url("https://example.com/") == "https://example.com"

    def test_strips_whitespace(self):
        assert setup_mod.normalize_url("  example.com  ") == "https://example.com"


# ---------------------------------------------------------------------------
# get_shell_rc_file()
# ---------------------------------------------------------------------------

class TestGetShellRcFile:

    @patch("platform.system", return_value="Darwin")
    @patch.dict(os.environ, {"SHELL": "/bin/zsh"})
    def test_macos_zsh(self, _):
        assert setup_mod.get_shell_rc_file() == Path.home() / ".zprofile"

    @patch("platform.system", return_value="Darwin")
    @patch.dict(os.environ, {"SHELL": "/bin/bash"})
    def test_macos_bash(self, _):
        assert setup_mod.get_shell_rc_file() == Path.home() / ".bash_profile"

    @patch("platform.system", return_value="Linux")
    @patch.dict(os.environ, {"SHELL": "/bin/zsh"})
    def test_linux_zsh(self, _):
        assert setup_mod.get_shell_rc_file() == Path.home() / ".zshrc"

    @patch("platform.system", return_value="Linux")
    @patch.dict(os.environ, {"SHELL": "/bin/bash"})
    def test_linux_bash(self, _):
        assert setup_mod.get_shell_rc_file() == Path.home() / ".bashrc"

    @patch("platform.system", return_value="Windows")
    def test_windows_returns_none(self, _):
        assert setup_mod.get_shell_rc_file() is None


# ---------------------------------------------------------------------------
# set_env_var / remove_env_var on Unix
# ---------------------------------------------------------------------------

class TestEnvVarUnix:

    def test_set_env_var_appends_to_rc(self, tmp_path):
        rc = tmp_path / ".zprofile"
        rc.write_text("")
        with patch.object(setup_mod, "get_shell_rc_file", return_value=rc), \
             patch("platform.system", return_value="Darwin"):
            success, msg = setup_mod.set_env_var("TEST_VAR", "test_value")
        assert success is True
        assert "export TEST_VAR=test_value" in rc.read_text()

    def test_set_env_var_idempotent(self, tmp_path):
        rc = tmp_path / ".zprofile"
        # Use the exact format that shlex.quote produces for simple values
        rc.write_text("export TEST_VAR=test_value\n")
        with patch.object(setup_mod, "get_shell_rc_file", return_value=rc), \
             patch("platform.system", return_value="Darwin"):
            success, _ = setup_mod.set_env_var("TEST_VAR", "test_value")
        assert success is True
        # Should not duplicate
        assert rc.read_text().count("export TEST_VAR=") == 1

    def test_remove_env_var_removes_line(self, tmp_path):
        rc = tmp_path / ".zprofile"
        rc.write_text("export FOO='bar'\nexport TEST_VAR='val'\nexport BAZ='qux'\n")
        with patch.object(setup_mod, "get_shell_rc_file", return_value=rc), \
             patch("platform.system", return_value="Darwin"):
            success, _ = setup_mod.remove_env_var("TEST_VAR")
        assert success is True
        content = rc.read_text()
        assert "TEST_VAR" not in content
        assert "FOO" in content
        assert "BAZ" in content


# ---------------------------------------------------------------------------
# append_to_file()
# ---------------------------------------------------------------------------

class TestAppendToFile:

    def test_appends_new_line(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("existing\n")
        assert setup_mod.append_to_file(f, "new line") is True
        assert "new line" in f.read_text()

    def test_skips_existing_line(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("existing line\n")
        assert setup_mod.append_to_file(f, "existing line") is False

    def test_creates_file_if_missing(self, tmp_path):
        f = tmp_path / "new.txt"
        assert setup_mod.append_to_file(f, "hello") is True
        assert f.exists()
        assert "hello" in f.read_text()
