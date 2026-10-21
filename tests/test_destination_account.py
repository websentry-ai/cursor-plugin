"""Shell commands report the account they reached: the git host and owner, the gh login, the ssh host and user.

Runs real git and ssh against throwaway repos and ssh configs, through the hook's own exchange builder.
"""
import importlib.util
import io
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("unbound_hook", _ROOT / "hooks" / "unbound.py")
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

pytestmark = pytest.mark.skipif(not (shutil.which("git") and shutil.which("ssh")), reason="needs git and ssh")

# Not a real credential; it only has to sit in a remote URL.
FAKE_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"  # gitleaks:allow


@pytest.fixture(autouse=True)
def _no_gh_login(tmp_path, monkeypatch):
    """Lookups inherit the environment, so no gh login leaks in from the machine running the tests."""
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_HOST", "GH_REPO"):
        monkeypatch.delenv(key, raising=False)


def _repo(tmp_path, remotes):
    path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    for name, url in remotes.items():
        subprocess.run(["git", "-C", str(path), "remote", "add", name, url], check=True)
    return str(path)


def _shell_entries(*commands, cwd=None, roots=None):
    """The shell tool uses one turn produces, with each command run from cwd or the workspace root."""
    base = {"conversation_id": "c1", "generation_id": "g1", "workspace_roots": roots or []}
    events = [{"event": {**base, "hook_event_name": "beforeSubmitPrompt", "prompt": "ship it"}}]
    for command in commands:
        shell = {**base, "hook_event_name": "afterShellExecution", "command": command, "output": ""}
        if cwd:
            shell["cwd"] = cwd
        events.append({"event": shell})
    events.append({"event": {**base, "hook_event_name": "afterAgentResponse", "text": "done"}})
    exchange = hook.build_llm_exchange(events)
    return exchange, exchange["messages"][-1]["tool_use"]


def test_a_push_reports_the_remote_host_and_owner_without_the_login(tmp_path):
    repo = _repo(tmp_path, {"origin": "https://github.com/acme/app.git",
                            "personal": f"https://jdoe:{FAKE_TOKEN}@github.com/jdoe-dev/app.git"})
    exchange, (entry,) = _shell_entries("git push personal main", cwd=repo)
    assert entry["destination_account"] == {"platform": "github", "host": "github.com", "account": "jdoe-dev",
                                            "context": "personal", "source": "command"}
    assert FAKE_TOKEN not in json.dumps(exchange)


def test_the_workspace_root_stands_in_when_the_event_has_no_cwd(tmp_path):
    repo = _repo(tmp_path, {"origin": "git@gitlab.com:acme/app.git"})
    _, (entry,) = _shell_entries("git push", roots=[repo])
    account = entry["destination_account"]
    assert (account["platform"], account["account"], account["context"]) == ("gitlab", "acme", "origin")


def test_ssh_resolves_the_real_host_user_and_port(tmp_path):
    config = tmp_path / "ssh_config"
    config.write_text("Host prod-bastion\n  HostName 10.0.4.12\n  User deploy\n  Port 2222\n")
    _, (entry,) = _shell_entries(f"ssh -F {config} prod-bastion uptime")
    account = entry["destination_account"]
    assert (account["platform"], account["host"], account["user"]) == ("ssh", "10.0.4.12:2222", "deploy")


def test_commands_that_reach_no_account_carry_none(tmp_path):
    repo = _repo(tmp_path, {"origin": "https://github.com/acme/app.git"})
    _, entries = _shell_entries("git status", "ls -la", "rsync -a dir host::module", cwd=repo)
    assert [e.get("destination_account") for e in entries] == [None, None, None]


def test_only_allowlisted_lookups_run_and_a_failed_lookup_leaves_the_account_unknown():
    ran = []

    def fake_run(argv, **kwargs):
        ran.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "")

    with patch.object(hook.subprocess, "run", side_effect=fake_run):
        _, (entry,) = _shell_entries("ssh prod-bastion 'cat ~/.aws/credentials'")
    assert entry["destination_account"] == {"platform": "ssh", "source": "unknown"}
    assert ran and all(argv[:2] == ["ssh", "-G"] for argv in ran), ran


def test_the_turn_sent_on_stop_carries_the_account(tmp_path):
    repo = _repo(tmp_path, {"origin": "https://github.com/jdoe-dev/app.git"})
    base = {"conversation_id": "c1", "generation_id": "g1", "workspace_roots": [repo]}
    turn = [{"hook_event_name": "beforeSubmitPrompt", "prompt": "ship it"},
            {"hook_event_name": "afterShellExecution", "command": "git push origin main", "output": "", "cwd": repo},
            {"hook_event_name": "afterAgentResponse", "text": "done"},
            {"hook_event_name": "stop", "status": "completed"}]
    sent = []
    with patch.object(hook, "AUDIT_LOG", tmp_path / "agent-audit.log"), \
         patch.object(hook, "ERROR_LOG", tmp_path / "error.log"), \
         patch.object(hook, "process_user_prompt_submit", return_value={}), \
         patch.object(hook, "send_to_api", side_effect=lambda exchange, key: sent.append(exchange)):
        for event in turn:
            with patch.object(hook.sys, "stdin", io.StringIO(json.dumps({**base, **event}))), \
                 patch.object(hook.sys, "stdout", io.StringIO()):
                hook.main()
    (exchange,) = sent
    account = exchange["messages"][-1]["tool_use"][0]["destination_account"]
    assert (account["host"], account["account"], account["context"]) == ("github.com", "jdoe-dev", "origin")


def test_a_gh_repo_url_never_carries_its_token():
    _, (entry,) = _shell_entries(f"gh pr create -R https://jdoe:{FAKE_TOKEN}@github.com/acme/app --fill")
    account = entry["destination_account"]
    assert (account["host"], account["account"]) == ("github.com", "acme")
    assert FAKE_TOKEN.lower() not in json.dumps(account).lower()


def test_an_ssh_config_that_runs_commands_is_never_read(tmp_path):
    marker = tmp_path / "ran"
    config = tmp_path / "ssh_config"
    config.write_text(f'Match host * exec "touch {marker}"\n  HostName 10.9.9.9\n')
    _, (entry,) = _shell_entries(f"ssh -F {config} prod-bastion")
    assert entry["destination_account"] == {"platform": "ssh", "source": "unknown"}
    assert not marker.exists()


def test_a_token_in_a_gh_repo_without_a_scheme_never_reaches_the_account(monkeypatch):
    monkeypatch.setenv("GH_REPO", f"x@{FAKE_TOKEN}@github.com/acme/app")
    _, (entry,) = _shell_entries("gh pr list")
    assert FAKE_TOKEN.lower() not in json.dumps(entry.get("destination_account", {})).lower()


def test_the_cwd_noted_before_a_shell_command_is_where_it_resolves(tmp_path):
    repo = _repo(tmp_path, {"origin": "https://github.com/jdoe-dev/app.git"})
    base = {"conversation_id": "c1", "generation_id": "g1", "workspace_roots": [str(tmp_path / "elsewhere")]}
    log = tmp_path / "agent-audit.log"
    with patch.object(hook, "AUDIT_LOG", log):
        hook._note_shell_cwd({**base, "hook_event_name": "preToolUse", "tool_name": "Shell",
                              "tool_input": {"command": "git push origin main", "working_directory": repo}})
    noted = [json.loads(line) for line in log.read_text().splitlines()]
    events = [{"event": {**base, "hook_event_name": "beforeSubmitPrompt", "prompt": "ship it"}}, *noted,
              {"event": {**base, "hook_event_name": "afterShellExecution", "command": "git push origin main", "output": ""}},
              {"event": {**base, "hook_event_name": "afterAgentResponse", "text": "done"}}]
    entry = hook.build_llm_exchange(events)["messages"][-1]["tool_use"][0]
    assert entry["destination_account"]["account"] == "jdoe-dev"


def test_a_push_turns_unknown_when_its_remote_is_rewritten_later_in_the_turn(tmp_path):
    repo = _repo(tmp_path, {"personal": "https://github.com/decoy/app.git"})
    _, (push, rewrite) = _shell_entries("git push personal main",
                                        "git remote set-url personal https://github.com/decoy/app.git", cwd=repo)
    assert push["destination_account"] == {"platform": "git", "source": "unknown"}
