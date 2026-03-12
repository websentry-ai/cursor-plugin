"""Unit tests for setup.py — download, hooks setup, and env var management.

Covers:
  - download_file(): success, failure, timeout
  - setup_hooks(): downloads hooks.json + unbound.py, makes executable
  - clear_setup(): removes env var + downloaded files
  - set_env_var / remove_env_var: cross-platform env var persistence
"""

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

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
# download_file()
# ---------------------------------------------------------------------------

class TestDownloadFile:

    def test_success_returns_true(self, tmp_path):
        dest = tmp_path / "subdir" / "file.txt"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert setup_mod.download_file("https://example.com/f", dest) is True
        # Verify curl was called with the right args
        cmd = mock_run.call_args[0][0]
        assert "curl" in cmd
        assert str(dest) in cmd

    def test_failure_returns_false(self, tmp_path):
        dest = tmp_path / "file.txt"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=22, stderr=b"404 Not Found"
            )
            assert setup_mod.download_file("https://example.com/bad", dest) is False

    def test_timeout_returns_false(self, tmp_path):
        import subprocess
        dest = tmp_path / "file.txt"
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("curl", 30)):
            assert setup_mod.download_file("https://example.com/slow", dest) is False

    def test_creates_parent_directories(self, tmp_path):
        dest = tmp_path / "a" / "b" / "c" / "file.txt"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            setup_mod.download_file("https://example.com/f", dest)
        assert dest.parent.exists()


# ---------------------------------------------------------------------------
# setup_hooks()
# ---------------------------------------------------------------------------

class TestSetupHooks:

    @patch.object(setup_mod, "download_file")
    def test_downloads_both_files(self, mock_dl, tmp_path):
        mock_dl.return_value = True
        # Patch Path.home to use tmp_path
        fake_script = tmp_path / ".cursor" / "hooks" / "unbound.py"
        fake_script.parent.mkdir(parents=True, exist_ok=True)
        fake_script.write_text("#!/usr/bin/env python3\n")

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = setup_mod.setup_hooks()

        assert result is True
        assert mock_dl.call_count == 2
        # First call: hooks.json, second call: unbound.py
        urls = [c[0][0] for c in mock_dl.call_args_list]
        assert any("hooks.json" in u for u in urls)
        assert any("unbound.py" in u for u in urls)

    @patch.object(setup_mod, "download_file")
    def test_returns_false_on_hooks_json_failure(self, mock_dl):
        mock_dl.return_value = False
        with patch("pathlib.Path.home", return_value=Path("/tmp/test-setup")):
            assert setup_mod.setup_hooks() is False

    @patch.object(setup_mod, "download_file")
    def test_returns_false_on_script_failure(self, mock_dl, tmp_path):
        # First call (hooks.json) succeeds, second (unbound.py) fails
        mock_dl.side_effect = [True, False]
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert setup_mod.setup_hooks() is False


# ---------------------------------------------------------------------------
# clear_setup()
# ---------------------------------------------------------------------------

class TestClearSetup:

    @patch.object(setup_mod, "remove_env_var", return_value=(True, "Removed"))
    def test_removes_env_var(self, mock_rm):
        with patch("pathlib.Path.home", return_value=Path("/tmp/nonexistent")):
            setup_mod.clear_setup()
        mock_rm.assert_called_once_with("UNBOUND_CURSOR_API_KEY")

    def test_removes_downloaded_files(self, tmp_path):
        hooks_json = tmp_path / ".cursor" / "hooks.json"
        hooks_json.parent.mkdir(parents=True, exist_ok=True)
        hooks_json.write_text("{}")

        script = tmp_path / ".cursor" / "hooks" / "unbound.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/usr/bin/env python3\n")

        with patch("pathlib.Path.home", return_value=tmp_path), \
             patch.object(setup_mod, "remove_env_var", return_value=(True, "Removed")):
            setup_mod.clear_setup()

        assert not hooks_json.exists()
        assert not script.exists()


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
# URL constants
# ---------------------------------------------------------------------------

class TestConstants:

    def test_hooks_url_points_to_websentry(self):
        assert "websentry-ai/setup" in setup_mod.HOOKS_URL
        assert "hooks.json" in setup_mod.HOOKS_URL

    def test_script_url_points_to_websentry(self):
        assert "websentry-ai/setup" in setup_mod.SCRIPT_URL
        assert "unbound.py" in setup_mod.SCRIPT_URL
