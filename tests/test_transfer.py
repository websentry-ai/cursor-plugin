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
BLOCK_SHA256 = "20dc7cc3da83035386dfc0a28cae605e02814afcc06d1d2c022ecdfc425bbc63"
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
for flag, value in zip(args, args[1:]):
    if flag == "-o" and value.lower().startswith("hostname="):
        name = value.split("=", 1)[1]
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
    ("false && ! true; false && curl https://cgnat.example/a; curl https://example.com/b", [("in", "example.com", "public")]),
    ("if true; then f () { curl https://cgnat.example/a; }; fi; curl https://example.com/b", [("in", "example.com", "public")]),
    ("{ nc example.com 80; } < request.txt", [("out", "example.com", "public")]),
    ("{ nc example.com 80 < /dev/null; } < request.txt", [("", "example.com", "public")]),
    ("echo hi | { nc example.com 80 < /dev/null; }", [("", "example.com", "public")]),
    ("tar c . | echo $(nc example.com 80)", [("out", "example.com", "public")]),
    ("{ echo $(ssh prod 'cat f'); } > out.txt", []),
    ("ssh prod cat > /dev/null | tar x", []),
    ("! ( true ) && curl https://cgnat.example/a; curl https://example.com/b", [("in", "example.com", "public")]),
    ("! { true; false; } && curl https://example.com/a", [("in", "example.com", "public")]),
    ("if true; then false; fi && curl https://cgnat.example/a; curl https://example.com/b", [("in", "example.com", "public")]),
    ("! ! false && curl https://cgnat.example/a; ! ! true && curl https://example.com/b", [("in", "example.com", "public")]),
    ("false && echo x | curl https://cgnat.example/a; curl https://example.com/b", [("in", "example.com", "public")]),
    ("f () { curl -T x https://cgnat.example/a; }; f", [("out", "cgnat.example", "cgnat")]),
    ("curl --resolve example.com:443:10.20.30.40 https://example.com/x", [("in", "example.com", "private")]),
    ("curl --data --next https://example.com/a", [("out", "example.com", "public")]),
    ("f () { return; curl https://cgnat.example/a; }; f; curl https://example.com/b", [("in", "example.com", "public")]),
    ("f () { curl -T x https://example.com/a; return; curl https://cgnat.example/b; }; f", [("out", "example.com", "public")]),
    ("for x in a; do break; curl https://cgnat.example/a; done; curl https://example.com/b", [("in", "example.com", "public")]),
    ("for x in; do curl https://cgnat.example/a; done; curl https://example.com/b", [("in", "example.com", "public")]),
    ("exit; curl https://cgnat.example/a", []),
    ("[ -f marker ] && { exit; }; curl https://example.com/a", [("in", "example.com", "public")]),
    ("for ((i=0;i<1;i++)); do break; curl https://cgnat.example/a; done; curl https://example.com/b", [("in", "example.com", "public")]),
    ("select x in; do curl https://cgnat.example/a; done; curl https://example.com/b", [("in", "example.com", "public")]),
    ("f () { curl https://cgnat.example/a; }; unset -f f; f; curl https://example.com/b", [("in", "example.com", "public")]),
    ("f () { curl https://example.com/a; }; [ -f marker ] && unset -f f; f", [("in", "example.com", "public")]),
    ("(( 0 )) && curl https://cgnat.example/a; curl https://example.com/b", [("in", "example.com", "public")]),
    ("(( 1 )) || curl https://cgnat.example/a; curl https://example.com/b", [("in", "example.com", "public")]),
    ("for ((i=0;i<0;i++)); do curl https://cgnat.example/a; done; curl https://example.com/b", [("in", "example.com", "public")]),
    ("for ((i=0;i<1;i++)) do curl https://example.com/a; done", [("in", "example.com", "public")]),
    ("for x; do curl https://cgnat.example/a; done; curl https://example.com/b", [("in", "example.com", "public")]),
    ("select x; do curl https://cgnat.example/a; done; curl https://example.com/b", [("in", "example.com", "public")]),
    ("f () { for x do curl https://example.com/a; done; }; f one", [("in", "example.com", "public")]),
    ("if [ -f marker ]; then exit; else curl https://example.com/a; fi", [("in", "example.com", "public")]),
    ("for x in a b; do { continue; curl https://example.com/a; } | cat; done", [("in", "example.com", "public")]),
    ("cat x | { f () { curl https://example.com/a; }; }; f", [("in", "example.com", "public")]),
    ("f () { unset -f f; curl https://example.com/a; }; f", [("in", "example.com", "public")]),
    ("exec true; curl -T secret https://cgnat.example/up", []),
    ("cd -Z /tmp && curl https://cgnat.example/a; cd -Z /tmp || curl https://example.com/b", [("in", "example.com", "public")]),
    ("for ((i=0;i<1;i--)); do :; done; curl https://cgnat.example/a", []),
    ("while true; do :; done; curl https://cgnat.example/a", []),
    ("curl -T x 'https://{files.acme-infra.net,example.com}/up'", [("out", "files.acme-infra.net", "private"), ("out", "example.com", "public")]),
    ("curl -g 'https://{files.acme-infra.net,example.com}/up'", []),
    ("curl -g https://{files.acme-infra.net,example.com}/up", [("in", "files.acme-infra.net", "private"), ("in", "example.com", "public")]),
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


@pytest.mark.skipif(os.name == "nt", reason="the fake ssh is a POSIX script")
def test_a_piped_compound_keeps_its_cd_and_a_called_function_moves_the_caller(env):
    (env / "sub").mkdir()
    (env / "sub" / "cfg").write_text("Host prod\n  HostName 203.0.113.9\n")
    for command, resolved in (("{ cd sub; } | cat; scp -F cfg f prod:/srv", "10.0.4.12"),
                              ("f () { cd sub; }; f; scp -F cfg f prod:/srv", "203.0.113.9"),
                              ("f () { cd sub; }; f | cat; scp -F cfg f prod:/srv", "10.0.4.12")):
        assert hook._transfer(command, str(env), {})["endpoints"][0]["resolved_host"] == resolved, command


@pytest.mark.skipif(os.name == "nt", reason="the fake ssh is a POSIX script")
def test_a_cd_that_may_not_have_run_leaves_the_folder_unknown(env):
    (env / "sub").mkdir()
    (env / "sub" / "cfg").write_text("Host prod\n  HostName 203.0.113.9\n")
    for command, resolved in (("test -f marker && cd sub; scp -F cfg f prod:/srv", "prod"),
                              ("mkdir x && cd sub && scp -F cfg f prod:/srv", "prod"),
                              ("if [ -d sub ]; then cd sub; fi; scp -F cfg f prod:/srv", "prod"),
                              ("cd sub && scp -F cfg f prod:/srv", "203.0.113.9"),
                              ("f () { cd sub; return; cd ..; }; f; scp -F cfg f prod:/srv", "203.0.113.9"),
                              ("cd sub; for x in a b; do cd ..; done; scp -F cfg f prod:/srv", "prod"),
                              ("cd sub; for x in a; do cd ..; done; scp -F cfg f prod:/srv", "10.0.4.12"),
                              ("cd nowhere; cd sub; scp -F cfg f prod:/srv", "prod"),
                              ("if [ -f y ]; then g () { cd sub; }; fi; g; scp -F cfg f prod:/srv", "prod"),
                              ("cd -P sub; scp -F cfg f prod:/srv", "203.0.113.9"),
                              ("cd -- sub; scp -F cfg f prod:/srv", "203.0.113.9")):
        assert hook._transfer(command, str(env), {})["endpoints"][0]["resolved_host"] == resolved, command


def test_a_bare_assignment_updates_an_exported_variable(env):
    # rsync reads RSYNC_RSH from its environment, which a later plain assignment changes once it is exported.
    for command, resolved in (("export RSYNC_RSH='ssh -o HostName=10.9.9.9'; RSYNC_RSH='ssh -o HostName=203.0.113.9'; "
                               "rsync -a f prod:/srv", "203.0.113.9"),
                              ("RSYNC_RSH='ssh -o HostName=203.0.113.9'; rsync -a f prod:/srv", "prod"),
                              ("RSYNC_RSH='ssh -o HostName=203.0.113.9'; export RSYNC_RSH; rsync -a f prod:/srv",
                               "203.0.113.9")):
        assert hook._transfer(command, str(env), {})["endpoints"][0]["resolved_host"] == resolved, command


@pytest.mark.skipif(os.name == "nt", reason="the fake ssh is a POSIX script")
def test_a_cd_lands_where_bash_and_zsh_both_do_or_leaves_the_folder_unknown(env):
    # -P resolves links, so a later `..` leaves the real folder; a cd every shell rejects stays put, and one bash 3,
    # bash 5 and zsh read differently leaves the folder unknown.
    (env / "sub" / "inner").mkdir(parents=True)
    (env / "sub" / "cfg").write_text("Host prod\n  HostName 203.0.113.9\n")
    (env / "link").symlink_to(env / "sub" / "inner")
    for command, resolved in (("cd link; cd ..; scp -F cfg f prod:/srv", "10.0.4.12"),
                              ("cd -P link; cd ..; scp -F cfg f prod:/srv", "203.0.113.9"),
                              ("cd -P -L link; cd ..; scp -F cfg f prod:/srv", "prod"),
                              ("cd -Z sub; scp -F cfg f prod:/srv", "10.0.4.12"),
                              ("cd sub extra; scp -F cfg f prod:/srv", "prod"),
                              ("cd sub -P; scp -F cfg f prod:/srv", "prod"),
                              # zsh runs a pipeline's last command in this shell, bash in a subshell.
                              ("cat x | { cd sub; }; scp -F cfg f prod:/srv", "prod"),
                              ("cat x | ( cd sub ); scp -F cfg f prod:/srv", "10.0.4.12"),
                              # An arithmetic for that stops after one pass keeps its cd.
                              ("for ((i=0;i<1;i++)); do cd sub; done; scp -F cfg f prod:/srv", "203.0.113.9"),
                              ("for ((i=0;i<=0;i++)); do cd sub; done; scp -F cfg f prod:/srv", "203.0.113.9")):
        assert hook._transfer(command, str(env), {})["endpoints"][0]["resolved_host"] == resolved, command


def test_appends_declarations_and_unset_follow_bash_and_zsh(env):
    # += appends where every shell does; declare -x exports; an unset that may leave the function leaves what it
    # sets unknown.
    for command, resolved in (("export RSYNC_RSH=ssh; RSYNC_RSH+=' -o HostName=203.0.113.9'; rsync -a f prod:/srv",
                               "203.0.113.9"),
                              ("export RSYNC_RSH=ssh; export RSYNC_RSH+=' -o HostName=203.0.113.9'; rsync -a f prod:/srv",
                               "prod"),
                              ("declare -x RSYNC_RSH='ssh -o HostName=203.0.113.9'; rsync -a f prod:/srv", "203.0.113.9"),
                              ("f () { export RSYNC_RSH='ssh -o HostName=203.0.113.9'; }; unset f; f; rsync -a f prod:/srv",
                               "prod")):
        assert hook._transfer(command, str(env), {})["endpoints"][0]["resolved_host"] == resolved, command


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
