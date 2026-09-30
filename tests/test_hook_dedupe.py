"""When Unbound's enterprise (MDM) hook is installed for Cursor, this plugin's
hook must defer — both fire on the same events, so running both logs and enforces
every event twice (the doubled-prompt bug). See WEB-6017.
"""
import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("unbound_hook", _ROOT / "hooks" / "unbound.py")
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


def _managed_dir_with(tmp_path, command):
    d = tmp_path / "Cursor"
    d.mkdir(parents=True, exist_ok=True)
    (d / "hooks.json").write_text(json.dumps(
        {"hooks": {"beforeSubmitPrompt": [{"command": command}]}}))
    return d


def test_managed_hook_present_true_for_unbound_managed(tmp_path):
    d = _managed_dir_with(tmp_path, '"/opt/unbound/current/unbound-hook/unbound-hook" hook cursor')
    with patch.object(hook, "_managed_cursor_hooks_dir", return_value=d):
        assert hook.managed_hook_present() is True


def test_managed_hook_present_false_for_another_orgs_hook(tmp_path):
    d = _managed_dir_with(tmp_path, "/opt/acme/their-hook.sh")
    with patch.object(hook, "_managed_cursor_hooks_dir", return_value=d):
        assert hook.managed_hook_present() is False


def test_managed_hook_present_false_when_no_managed_config(tmp_path):
    d = tmp_path / "Cursor"
    d.mkdir(parents=True, exist_ok=True)
    with patch.object(hook, "_managed_cursor_hooks_dir", return_value=d):
        assert hook.managed_hook_present() is False


def _run_main(event):
    with patch.object(sys, "stdin", io.StringIO(json.dumps(event))):
        out = io.StringIO()
        with patch.object(sys, "stdout", out):
            hook.main()
    return out.getvalue()


def test_main_defers_and_logs_nothing_when_managed_present():
    event = {"hook_event_name": "beforeSubmitPrompt", "conversation_id": "c1",
             "generation_id": "g1", "prompt": "now"}
    with patch.object(hook, "managed_hook_present", return_value=True), \
         patch.object(hook, "append_to_audit_log") as audit:
        out = _run_main(event)
    audit.assert_not_called()
    assert out.strip() == "{}"


def test_main_processes_when_managed_absent():
    event = {"hook_event_name": "afterAgentResponse", "conversation_id": "c1",
             "generation_id": "g1"}
    with patch.object(hook, "managed_hook_present", return_value=False), \
         patch.object(hook, "append_to_audit_log") as audit, \
         patch.object(hook, "send_to_api"), \
         patch.object(hook, "process_stop_event"):
        _run_main(event)
    audit.assert_called()
