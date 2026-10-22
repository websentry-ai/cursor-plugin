"""The hook records where each shell transfer went: the tool, and for each place it sent to or fetched from, the host
as written (no login), the host ssh resolves it to, and the kind of address that host has. Every tool entry also
carries the time its call was logged.

The transfer block is byte-identical to the one in websentry-ai/setup's hooks, whose own suite covers it in depth;
both repos pin its digest, so a change made in one and not the other fails here.
"""
import hashlib
import importlib.util
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).parent.parent
_HOOK_PATH = _ROOT / "hooks" / "unbound.py"
# Also pinned in websentry-ai/setup tests/test_transfer_destination.py; update both together.
BLOCK_SHA256 = "52a96102496501b4cac9df373593cee98bae601960eecf31ef8d41cd27ca8556"
START = "# KEEP IN SYNC: transfer resolution"
END = "# --- end transfer resolution ---"
# Not a real credential; it only has to sit where a login goes.
FAKE_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"  # gitleaks:allow
ADDRESSES = {"files.acme-infra.net": ["10.20.30.40"], "example.com": ["93.184.216.34"], "cgnat.example": ["100.64.1.1"]}
FAKE_SSH = """#!{python}
import os, sys
args = sys.argv[1:]
open(os.environ["FAKE_SSH_LOG"], "a").write(" ".join(args) + "\\n")
config = args[args.index("-F") + 1] if "-F" in args else None
host, name = args[-1], args[-1]
for line in open(config) if config else []:
    words = line.split()
    if words and words[0] == "Host":
        current = words[1:]
    elif words and words[0] == "HostName" and host in current:
        name = words[1]
print("hostname " + name)
"""


def _load_hook():
    # The hook makes its log folder under the home folder on import, so a throwaway one stands in.
    home = os.environ.get("HOME")
    os.environ["HOME"] = tempfile.mkdtemp()
    try:
        spec = importlib.util.spec_from_file_location("cursor_plugin_hook", _HOOK_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = home


hook = _load_hook()


def _getaddrinfo(host, *args, **kwargs):
    if host not in ADDRESSES:
        raise socket.gaierror(socket.EAI_NONAME, "unknown host")
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ADDRESSES[host]]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A fake ssh first on PATH, an empty ssh home, a resolver that knows only ADDRESSES, and a config for prod."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(FAKE_SSH.format(python=sys.executable))
    ssh.chmod(0o755)
    (tmp_path / "home" / ".ssh").mkdir(parents=True)
    (tmp_path / "cfg").write_text("Host prod\n  HostName 10.0.4.12\n")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("FAKE_SSH_LOG", str(tmp_path / "ssh.log"))
    monkeypatch.setattr(hook, "_transfer_home", lambda: str(tmp_path / "home"))
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    return tmp_path


def _block():
    text = _HOOK_PATH.read_text()
    assert text.count(START) == 1 and text.count(END) == 1
    return text[text.index(START):text.index(END) + len(END)]


def test_the_block_matches_the_digest_setup_pins():
    assert hashlib.sha256(_block().encode("utf-8")).hexdigest() == BLOCK_SHA256


@pytest.mark.skipif(os.name == "nt", reason="the fake ssh is a POSIX script")
def test_a_shell_call_carries_its_transfer_and_every_entry_its_call_time(env):
    roots = {"conversation_id": "c1", "generation_id": "g1"}
    events = [
        {"timestamp": "2026-10-22T22:09:59+05:30", "event": {"hook_event_name": "beforeSubmitPrompt", "prompt": "ship it", **roots}},
        {"timestamp": "2026-10-22T22:10:00.700+05:30",
         "event": {"hook_event_name": "afterShellExecution", "command": "scp -F cfg build.zip prod:/srv", "cwd": str(env),
                   "output": "", **roots}},
        {"timestamp": "2026-10-22T22:10:01+05:30",
         "event": {"hook_event_name": "afterFileEdit", "file_path": "/tmp/x", "edits": [], **roots}},
        {"timestamp": "2026-10-22T22:10:02+05:30", "event": {"hook_event_name": "afterAgentResponse", "text": "done", **roots}},
    ]
    shell, edit = hook.build_llm_exchange(events)["messages"][1]["tool_use"]
    assert shell["transfer"] == {"tool": "scp", "endpoints": [
        {"direction": "out", "host": "prod", "resolved_host": "10.0.4.12", "address": "private"}]}
    assert shell["ts"] == "2026-10-22T16:40:00Z"
    assert edit["ts"] == "2026-10-22T16:40:01Z" and "transfer" not in edit
    assert (env / "ssh.log").read_text() == "-G -F cfg -- prod\n"


@pytest.mark.parametrize("command, expected", [
    ("curl -T build.zip https://files.acme-infra.net/up", [("out", "files.acme-infra.net", "private")]),
    ("curl -fsSLo a.tgz https://example.com/a.tgz", [("in", "example.com", "public")]),
    ("wget --post-file=a https://cgnat.example/u", [("out", "cgnat.example", "cgnat")]),
    ("echo hi | nc example.com 80", [("out", "example.com", "public")]),
    ("Invoke-RestMethod https://example.com/api -Method Post -Body $b", [("out", "example.com", "public")]),
    ("false && (echo x; curl https://cgnat.example/a); curl https://example.com/b", [("in", "example.com", "public")]),
    ("if false; then curl https://cgnat.example/a; else curl https://example.com/b; fi", [("in", "example.com", "public")]),
    ("curl --upload-file=build.zip https://files.acme-infra.net/up", [("out", "files.acme-infra.net", "private")]),
    ("tar c . | sh -c 'nc example.com 80'", [("out", "example.com", "public")]),
    ("curl https://faß.de/a", [("in", "xn--fa-hia.de", "unresolved")]),
    ("if false; then echo $(curl https://cgnat.example/a); fi; curl https://example.com/b", [("in", "example.com", "public")]),
    ("curl -T a https://files.acme-infra.net/up --next https://example.com/b", [("out", "files.acme-infra.net", "private"), ("in", "example.com", "public")]),
    ("f () { curl -T x https://cgnat.example/a; }; curl https://example.com/b", [("in", "example.com", "public")]),
    ("false && ! echo $(curl https://cgnat.example/a); curl https://example.com/b", [("in", "example.com", "public")]),
    ("ls -la", []),
])
def test_tools_directions_and_the_shell_reading(env, command, expected):
    transfer = hook._transfer(command, str(env), {})
    got = [(e.get("direction", ""), e["host"], e["address"]) for e in (transfer or {}).get("endpoints", [])]
    assert got == expected


@pytest.mark.skipif(os.name == "nt", reason="the fake ssh is a POSIX script")
def test_sudo_around_a_command_string_never_looks_up_ssh(env):
    transfer = hook._transfer("sudo -u deploy sh -c 'scp -F cfg build.zip prod:/srv'", str(env), {})
    assert transfer["endpoints"] == [{"direction": "out", "host": "prod", "resolved_host": "prod", "address": "unresolved"}]
    assert not (env / "ssh.log").exists()
    assert hook._transfer("scp -F cfg build.zip prod:/srv", str(env), {})["endpoints"][0]["resolved_host"] == "10.0.4.12"


def test_no_login_or_token_reaches_the_field(env):
    for command in (f"curl https://jdoe:{FAKE_TOKEN}@example.com/x", f"wget -O x https://jdoe:{FAKE_TOKEN}@example.com/x"):
        transfer = hook._transfer(command, str(env), {})
        assert transfer["endpoints"][0]["host"] == "example.com"
        assert FAKE_TOKEN not in json.dumps(transfer) and "jdoe" not in json.dumps(transfer)


def test_a_hanging_lookup_is_cut_off_and_an_error_sends_no_field(env, monkeypatch):
    def hang(*args, **kwargs):
        time.sleep(2)
        raise socket.gaierror("slow")

    monkeypatch.setattr(socket, "getaddrinfo", hang)
    monkeypatch.setattr(hook, "_TRANSFER_DNS_TIMEOUT", 0.2)
    started = time.monotonic()
    transfer = hook._transfer("curl https://slow.example/x", str(env), {})
    assert time.monotonic() - started < 1.0
    assert transfer["endpoints"][0]["address"] == "unresolved"
    with patch.object(hook, "_transfer_walk", side_effect=RuntimeError):
        assert hook._transfer("curl https://example.com", str(env), {}) is None
