"""The pre-tool check names the device's OS, which the gateway needs to read `/ETC/x` as /etc on macOS."""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_spec = importlib.util.spec_from_file_location(
    'unbound_hook', Path(__file__).resolve().parents[1] / 'hooks' / 'unbound.py'
)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


def _sent_body(event):
    with patch.object(hook, 'send_to_hook_api', return_value={}) as send, \
            patch.object(hook, 'get_latest_user_prompt', return_value=None):
        hook.process_pre_tool_use(event, 'key')
    return send.call_args[0][0]


def test_the_check_names_this_machines_os():
    expected = {'darwin': 'macos', 'linux': 'linux', 'win32': 'windows'}[sys.platform]
    body = _sent_body({'tool_name': 'Write', 'tool_input': {'file_path': '/ETC/sudoers'}})
    assert body['pre_tool_use_data']['os'] == expected


@pytest.mark.parametrize('system, expected', [('Darwin', 'macos'), ('Linux', 'linux'), ('Windows', 'windows')])
def test_the_three_desktop_systems(system, expected):
    with patch.object(hook.platform, 'system', return_value=system):
        assert hook.device_os() == expected


@pytest.mark.parametrize('system', ['FreeBSD', '', 'darwin'])
def test_no_os_is_sent_for_any_other_system(system):
    with patch.object(hook.platform, 'system', return_value=system):
        assert 'os' not in _sent_body({'tool_name': 'Shell', 'command': 'ls'})['pre_tool_use_data']


def test_a_failing_probe_sends_no_os():
    with patch.object(hook.platform, 'system', side_effect=OSError('no uname')):
        assert 'os' not in _sent_body({'tool_name': 'Shell', 'command': 'ls'})['pre_tool_use_data']
