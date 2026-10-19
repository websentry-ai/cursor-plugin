#!/usr/bin/env python3
"""
Real-time Cursor hook event processor with smart garbage collection.
Reads JSON events from stdin, appends to agent-audit.log, and processes them on stop events.
"""

import sys
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional


UNBOUND_GATEWAY_URL = "https://api.getunbound.ai"

# Use user's home directory for logs
LOG_DIR = Path.home() / ".cursor" / "hooks"
AUDIT_LOG = LOG_DIR / "agent-audit.log"
ERROR_LOG = LOG_DIR / "error.log"


# Ensure log directory exists
try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    # Fallback to temp directory if home directory is not writable
    import tempfile
    LOG_DIR = Path(tempfile.gettempdir()) / "cursor-hooks"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT_LOG = LOG_DIR / "agent-audit.log"
    ERROR_LOG = LOG_DIR / "error.log"


def log_error(message):
    """Log error with timestamp to error.log, keeping only last 25 errors."""
    try:
        timestamp = datetime.now().astimezone().isoformat().replace('+00:00', 'Z')
        error_entry = f"{timestamp}: {message}\n"

        with open(ERROR_LOG, 'a', encoding='utf-8') as f:
            f.write(error_entry)

        # Keep only last 25 errors
        if ERROR_LOG.exists():
            with open(ERROR_LOG, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            if len(lines) > 25:
                with open(ERROR_LOG, 'w', encoding='utf-8') as f:
                    f.writelines(lines[-25:])
    except Exception:
        pass  # Never let error logging break the hook


def load_existing_logs():
    """Load existing logs from agent-audit.log into memory."""
    logs = []
    if AUDIT_LOG.exists():
        with open(AUDIT_LOG, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        logs.append(json.loads(line))
                    except json.JSONDecodeError: 
                        continue
    return logs


def save_logs(logs):
    """Save logs back to agent-audit.log."""
    with open(AUDIT_LOG, 'w', encoding='utf-8') as f:
        for log in logs:
            f.write(json.dumps(log) + '\n')


def append_to_audit_log(event_data):
    """Append event to agent-audit.log."""
    with open(AUDIT_LOG, 'a', encoding='utf-8') as f:
        f.write(json.dumps(event_data) + '\n')


def group_events_by_generation(logs):
    """Group events by conversation_id and generation_id."""
    grouped = defaultdict(lambda: defaultdict(list))
    
    for log in logs:
        event = log.get('event', {})
        conversation_id = event.get('conversation_id')
        generation_id = event.get('generation_id')
        
        if conversation_id and generation_id:
            grouped[conversation_id][generation_id].append(log)
    
    return grouped


def get_latest_user_prompt(generation_id):
    """Get the most recent user prompt for this generation from logs."""
    logs = load_existing_logs()
    latest_prompt = None

    for log in logs:
        event = log.get('event', {})
        if (event.get('hook_event_name') == 'beforeSubmitPrompt' and
            event.get('generation_id') == generation_id):
            latest_prompt = event.get('prompt')

    return latest_prompt


def extract_command_for_pretool(event):
    """Extract command from tool_input based on tool type."""
    tool_input = event.get('tool_input', {})
    tool_name = event.get('tool_name', '')

    # Shell/Bash: command field
    if tool_name in ['Shell', 'Bash'] and 'command' in tool_input:
        return tool_input['command']
    # File operations: file_path
    if 'file_path' in tool_input:
        return tool_input['file_path']
    # MCP tools: stringify the input
    if tool_name == 'MCP':
        return str(tool_input)
    # Default: tool name
    return tool_name


def send_to_hook_api(request_body, api_key):
    """Send request to /v1/hooks/pretool endpoint."""
    if not api_key:
        return {}

    try:
        url = f"{UNBOUND_GATEWAY_URL}/v1/hooks/pretool"
        data = json.dumps(request_body)

        result = subprocess.run(
            ["curl", "-fsSL", "-X", "POST",
             "-H", f"Authorization: Bearer {api_key}",
             "-H", "Content-Type: application/json",
             "-d", data, url],
            capture_output=True,
            timeout=10
        )

        if result.returncode == 0 and result.stdout:
            return json.loads(result.stdout.decode('utf-8'))
        return {}
    except Exception as e:
        log_error(f"Hook API error: {str(e)}")
        return {}


def process_pre_tool_use(event, api_key):
    """Process preToolUse event"""
    generation_id = event.get('generation_id')
    conversation_id = event.get('conversation_id')
    model = event.get('model') or 'auto'
    tool_name = event.get('tool_name', '')

    user_prompt = get_latest_user_prompt(generation_id)
    command = extract_command_for_pretool(event)

    request_body = {
        'conversation_id': conversation_id,
        'unbound_app_label': 'cursor',
        'model': model,
        'event_name': 'tool_use',
        'pre_tool_use_data': {
            'tool_name': tool_name,
            'command': command,
            'metadata': event
        },
        'messages': [{'role': 'user', 'content': user_prompt}] if user_prompt else []
    }

    api_response = send_to_hook_api(request_body, api_key)
    return api_response if api_response else {}


def process_user_prompt_submit(event, api_key):
    """Process beforeSubmitPrompt event for policy checking"""
    conversation_id = event.get('conversation_id')
    model = event.get('model') or 'auto'
    prompt = event.get('prompt', '')

    request_body = {
        'conversation_id': conversation_id,
        'unbound_app_label': 'cursor',
        'model': model,
        'event_name': 'user_prompt',
        'messages': [{'role': 'user', 'content': prompt}] if prompt else []
    }

    api_response = send_to_hook_api(request_body, api_key)
    return api_response if api_response else {}


# KEEP IN SYNC: destination-account resolution — byte-identical in every agent hook, here and in cursor-plugin, do not diverge.
# --- Destination account: which account a shell command reached, identity fields only, fail-open ---

# Only these commands are ever run to resolve an account; everything else is refused.
_ACCOUNT_ALLOWED_RUNS = (
    ('git', 'remote', 'get-url', '--push'),
    ('git', 'rev-parse', '--abbrev-ref', '--symbolic-full-name'),
    ('git', 'config', '--get'),
    ('gh', 'config', 'get', '-h'),
    ('ssh', '-G'),
)
_ACCOUNT_RUN_TIMEOUT = 5
# Wall-clock budget for all lookups in one turn, so a slow tool never delays the agent.
_ACCOUNT_TURN_BUDGET = 10.0
_ACCOUNT_WRAPPERS = frozenset({'sudo', 'env', 'command', 'exec', 'time', 'nohup'})
_ACCOUNT_SSH_TOOLS = frozenset({'ssh', 'scp', 'sftp', 'rsync'})
# Options that take a value, per program; every other option is a switch.
_ACCOUNT_SSH_VALUE_FLAGS = {'ssh': frozenset('BbcDEeFIiJLlmOoPpQRSWw'), 'scp': frozenset('cDFiJloPSX'),
                            'sftp': frozenset('BbcDFiJloPRSsX')}
_ACCOUNT_GH_HOSTS_ENV = ('GH_HOST',)
_ACCOUNT_GH_TOKEN_ENV = ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN')


def _account_run(argv: List[str], cwd: Optional[str], state: Dict) -> Optional[str]:
    """Run one allowlisted identity lookup with a timeout; None when refused, slow, failing or over budget."""
    try:
        if not any(tuple(_account_strip_git_dir(argv)[:len(p)]) == p for p in _ACCOUNT_ALLOWED_RUNS):
            return None
        deadline = state.setdefault('deadline', time.monotonic() + _ACCOUNT_TURN_BUDGET)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        result = subprocess.run(argv, cwd=cwd if cwd and os.path.isdir(cwd) else None, capture_output=True,
                                text=True, timeout=min(_ACCOUNT_RUN_TIMEOUT, remaining), stdin=subprocess.DEVNULL)
        return result.stdout if result.returncode == 0 else None
    except Exception:
        return None


def _account_strip_git_dir(argv: List[str]) -> List[str]:
    """argv without git's leading -C <dir>, so the allowlist matches the subcommand."""
    if len(argv) >= 3 and argv[0] == 'git' and argv[1] == '-C':
        return [argv[0]] + argv[3:]
    return argv


def _account_segments(command: str) -> List[List[str]]:
    """The command split into simple commands on ; && || | and newlines, each as argv; [] when it can't be parsed."""
    try:
        # A backslash-newline continues the line; a comment swallows its newline, so a doubled one survives it.
        text = command.replace('\\\n', ' ').replace('\n', '\n\n')
        lexer = shlex.shlex(text, posix=True, punctuation_chars=';&|\n')
        lexer.whitespace_split = True
        lexer.whitespace = ' \t\r'
        segments, current = [], []
        for token in lexer:
            if token and set(token) <= set(';&|\n'):
                if current:
                    segments.append(current)
                current = []
            else:
                current.append(token)
        if current:
            segments.append(current)
        return segments
    except Exception:
        return []


def _account_program(argv: List[str]) -> tuple:
    """(program, its args, inline VAR=value settings) after leading assignments and wrappers like sudo or env."""
    env, i = {}, 0
    while i < len(argv):
        token = argv[i]
        if '=' in token and not token.startswith('-') and token.split('=', 1)[0].replace('_', '').isalnum():
            key, value = token.split('=', 1)
            env[key] = value
        elif os.path.basename(token) in _ACCOUNT_WRAPPERS:
            pass
        elif token.startswith('-') and i > 0 and os.path.basename(argv[i - 1]) in _ACCOUNT_WRAPPERS:
            pass
        else:
            return os.path.basename(token), argv[i + 1:], env
        i += 1
    return None, [], env


def _account_strip_login(url: str) -> str:
    """A remote URL with any user:password@ removed."""
    return re.sub(r'^([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@]*@', r'\1', url.strip())


def _account_remote_parts(url: str) -> tuple:
    """(host, owner, repo) from an https or scp-style git remote; empty strings for what it doesn't name."""
    url = _account_strip_login(url)
    match = re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://([^/:]+)(?::\d+)?/(.*)$', url)
    if not match:
        match = re.match(r'^(?:[^@/:]+@)?([^/:]+):(?!//)(.*)$', url)
    if not match:
        return '', '', ''
    host, path = match.group(1).lower(), match.group(2).strip('/')
    if path.endswith('.git'):
        path = path[:-4]
    parts = [p for p in path.split('/') if p]
    owner = parts[0] if parts else ''
    repo = parts[-1] if len(parts) > 1 else ''
    return host, owner, repo


def _account_platform_for_host(host: str) -> str:
    if host == 'github.com' or host.endswith('.ghe.com'):
        return 'github'
    if host == 'gitlab.com':
        return 'gitlab'
    return 'git'


def _account_gh_user(host: str, env: Dict, cwd: Optional[str], state: Dict) -> str:
    """The gh login for a host, read from gh's local config; empty when a token variable overrides the stored login."""
    if any(env.get(k) or os.environ.get(k) for k in _ACCOUNT_GH_TOKEN_ENV):
        return ''
    key = ('gh-user', host)
    if key not in state:
        out = _account_run(['gh', 'config', 'get', '-h', host, 'user'], cwd, state)
        state[key] = (out or '').strip().splitlines()[0] if out and out.strip() else ''
    return state[key]


def _account_git_push(args: List[str], env: Dict, cwd: Optional[str], state: Dict) -> Optional[Dict]:
    """The account a `git push` reaches: the push remote's host and owner, and the gh login pushing over https."""
    git_dir, rest = cwd, list(args)
    while rest and rest[0].startswith('-'):
        flag = rest.pop(0)
        if flag == '-C' and rest:
            path = os.path.expanduser(rest.pop(0))
            git_dir = path if os.path.isabs(path) else (os.path.join(git_dir, path) if git_dir else None)
        elif flag in ('-c', '--git-dir', '--work-tree', '--namespace') and rest:
            rest.pop(0)
    if not rest or rest[0] != 'push':
        return None
    target, positional, push_args, i = None, [], rest[1:], 0
    while i < len(push_args):
        arg = push_args[i]
        if arg in ('-o', '--push-option', '--receive-pack', '--exec'):
            i += 2
            continue
        if arg == '--repo' and i + 1 < len(push_args):
            target = push_args[i + 1]
            i += 2
            continue
        if arg.startswith('--repo='):
            target = arg.split('=', 1)[1]
        elif not arg.startswith('-'):
            positional.append(arg)
        i += 1
    target = target or (positional[0] if positional else None)
    base = ['git', '-C', git_dir or '.']
    if target and re.search(r'://|^[^/:]+@[^/:]+:|^[^/:]+:[^/]', target):
        url, remote = target, ''
    elif not git_dir:
        # Without the folder the push ran in, its remotes can't be read.
        return {'platform': 'git', 'source': 'unknown'}
    else:
        remote = target
        if not remote:
            out = _account_run(base + ['rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{push}'], git_dir, state)
            remote = out.strip().split('/', 1)[0] if out and '/' in out else 'origin'
        url = _account_run(base + ['remote', 'get-url', '--push', remote], git_dir, state)
    host, owner, _repo = _account_remote_parts(url.strip().splitlines()[0]) if url else ('', '', '')
    if not host:
        return {'platform': 'git', 'source': 'unknown'}
    user = ''
    if re.match(r'^https?://', url.strip()):
        helper = _account_run(base + ['config', '--get', f'credential.https://{host}.helper'], git_dir, state) or \
            _account_run(base + ['config', '--get', 'credential.helper'], git_dir, state) or ''
        if 'auth git-credential' in helper:
            user = _account_gh_user(host, env, git_dir, state)
    return {'platform': _account_platform_for_host(host), 'host': host, 'account': owner, 'user': user,
            'context': remote or '', 'source': 'command'}


def _account_gh(args: List[str], env: Dict, cwd: Optional[str], state: Dict) -> Optional[Dict]:
    """The account a gh command reaches: host and owner from -R, a repo argument, GH_REPO or the repo's remote, plus the gh login."""
    repo, flag_host = '', ''
    for i, arg in enumerate(args):
        if arg in ('-R', '--repo') and i + 1 < len(args):
            repo = args[i + 1]
        elif arg.startswith('--repo='):
            repo = arg.split('=', 1)[1]
        elif arg == '--hostname' and i + 1 < len(args):
            flag_host = args[i + 1].lower()
    # `gh repo view|clone|fork|… OWNER/REPO` and `gh api repos/OWNER/REPO/…` name the repository themselves.
    if not repo and len(args) > 2 and args[0] == 'repo' and not args[2].startswith('-') and '/' in args[2]:
        repo = args[2]
    if not repo and args[:1] == ['api']:
        named = next((m for m in (re.match(r'^/?repos/([^/{}]+/[^/{}]+)', a) for a in args[1:]) if m), None)
        if named:
            repo = named.group(1)
        elif not any('{owner}' in a or '{repo}' in a for a in args):
            cwd = None  # an api path that names no repo reaches no repo of this checkout
    repo = repo or env.get('GH_REPO') or os.environ.get('GH_REPO') or ''
    env_host = (env.get('GH_HOST') or os.environ.get('GH_HOST') or '').lower()
    host, owner = '', ''
    if repo:
        repo = re.sub(r'^([a-zA-Z][a-zA-Z0-9+.-]*://)?[^/@]*@', r'\1', repo.strip())  # never keep a login or token
        if ':' in repo:
            host, owner, _repo = _account_remote_parts(repo)
        else:
            parts = [p for p in repo.split('/') if p]
            host, owner = (parts[0].lower(), parts[1]) if len(parts) >= 3 else ('', parts[0] if parts else '')
    elif cwd:
        url = _account_run(['git', '-C', cwd, 'remote', 'get-url', '--push', 'origin'], cwd, state)
        if url:
            host, owner, _repo = _account_remote_parts(url.strip().splitlines()[0])
    host = flag_host or host or env_host or 'github.com'
    return {'platform': 'github', 'host': host, 'account': owner, 'user': _account_gh_user(host, env, cwd, state),
            'context': '', 'source': 'command'}


def _account_ssh_destination(program: str, args: List[str]) -> tuple:
    """(destination, identity options to pass to ssh -G) for ssh, scp, sftp or rsync; (None, []) when there is none."""
    passthrough, positional, i = [], [], 0
    value_flags = _ACCOUNT_SSH_VALUE_FLAGS.get(program, frozenset())
    # Only the options that change which host or user ssh -G resolves are passed through; scp's -l is a bandwidth limit.
    identity_flags = ('l', 'o', 'F', 'J', 'p') if program == 'ssh' else ('o', 'F', 'J', 'P')
    while i < len(args):
        arg = args[i]
        if program == 'rsync':
            if arg in ('-e', '--rsh'):
                i += 2
                continue
            if not arg.startswith('-'):
                positional.append(arg)
            i += 1
            continue
        if arg.startswith('-') and len(arg) >= 2 and arg != '--':
            flag, attached = arg[1], arg[2:]
            takes_value = flag in value_flags
            value = attached if attached else (args[i + 1] if takes_value and i + 1 < len(args) else None)
            if takes_value and value is not None and flag in identity_flags:
                passthrough += ['-p' if flag == 'P' else '-' + flag, value]
            i += 1 if (attached or not takes_value) else 2
            continue
        positional.append(arg)
        i += 1
    if program == 'ssh':
        return (positional[0], passthrough) if positional else (None, [])
    # The last remote operand is where the copy goes.
    for arg in reversed(positional):
        if arg.startswith('rsync://') or '::' in arg:
            return None, []
        match = re.match(r'^((?:[^@/:]+@)?\[?[^/:\]]+\]?):', arg)
        if match and not re.match(r'^[A-Za-z]:\\', arg):
            return match.group(1), passthrough
    # sftp also takes a plain [user@]host with no path.
    if program == 'sftp' and positional and re.match(r'^(?:[^@/:]+@)?\[?[^/:\]]+\]?$', positional[0]):
        return positional[0], passthrough
    return None, []


def _account_ssh_config_runs_commands(passthrough: List[str], cwd: Optional[str]) -> bool:
    """Whether the ssh config that ssh -G would read has a Match exec, which ssh runs while resolving."""
    path = '~/.ssh/config'
    for flag, value in zip(passthrough[::2], passthrough[1::2]):
        if flag == '-F':
            path = value
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(cwd or '', path)
    try:
        with open(path, errors='ignore') as config:
            return any(re.match(r'\s*match\b.*\bexec\b', line, re.IGNORECASE) for line in config)
    except OSError:
        return False


def _account_ssh(program: str, args: List[str], cwd: Optional[str], state: Dict) -> Optional[Dict]:
    """The host and user an ssh-family command reaches, resolved through ssh -G."""
    destination, passthrough = _account_ssh_destination(program, args)
    if not destination:
        return None
    if _account_ssh_config_runs_commands(passthrough, cwd):
        return {'platform': 'ssh', 'source': 'unknown'}
    out = _account_run(['ssh', '-G'] + passthrough + [destination], cwd, state)
    if not out:
        return {'platform': 'ssh', 'source': 'unknown'}
    settings = {}
    for line in out.splitlines():
        key, _, value = line.strip().partition(' ')
        if key in ('hostname', 'user', 'port', 'proxyjump') and key not in settings:
            settings[key] = value.strip()
    host = settings.get('hostname', '').lower()
    if not host:
        return {'platform': 'ssh', 'source': 'unknown'}
    port = settings.get('port', '22')
    alias = destination.rsplit('@', 1)[-1].strip('[]')
    return {'platform': 'ssh', 'host': host if port in ('', '22') else f'{host}:{port}', 'account': '',
            'user': settings.get('user', ''), 'context': alias if alias.lower() != host else '', 'source': 'command'}


def _destination_account(command: Optional[str], cwd: Optional[str], state: Dict) -> Optional[Dict]:
    """The account the first resolved command in a shell call reached, else the first unresolved one, or None."""
    try:
        if not isinstance(command, str) or not command.strip():
            return None
        unresolved = None
        for argv in _account_segments(command):
            program, args, env = _account_program(argv)
            account = None
            if program == 'cd':
                # Later segments run in the folder this one moves to.
                target = os.path.expanduser(args[0]) if args else os.path.expanduser('~')
                cwd = target if os.path.isabs(target) else os.path.normpath(os.path.join(cwd or '', target))
                continue
            if program == 'git':
                account = _account_git_push(args, env, cwd, state)
            elif program == 'gh':
                account = _account_gh(args, env, cwd, state)
            elif program in _ACCOUNT_SSH_TOOLS:
                account = _account_ssh(program, args, cwd, state)
            if account:
                account = {k: v for k, v in account.items() if v or k in ('platform', 'source')}
                if account['source'] != 'unknown':
                    return account
                unresolved = unresolved or account
        return unresolved
    except Exception:
        return None
# --- end destination account ---


def build_llm_exchange(events, api_key=None):
    """Build standard LLM exchange format from events."""
    messages = []
    assistant_tool_uses = []
    
    user_prompt = None
    assistant_response = None
    conversation_id = None
    model = None
    account_state = {}
    
    for log_entry in events:
        event = log_entry.get('event', {})
        hook_event_name = event.get('hook_event_name')
        
        if not conversation_id:
            conversation_id = event.get('conversation_id')
        
        if not model:
            model = event.get('model')
        
        if hook_event_name == 'beforeSubmitPrompt':
            user_prompt = event.get('prompt')
        
        elif hook_event_name == 'beforeReadFile':
            assistant_tool_uses.append({
                'type': hook_event_name,
                'file_path': event.get('file_path'),
                'content': event.get('content', ''),
                'attachments': event.get('attachments', [])
            })
        
        elif hook_event_name == 'afterFileEdit':
            assistant_tool_uses.append({
                'type': hook_event_name,
                'file_path': event.get('file_path'),
                'edits': event.get('edits', [])
            })
        
        elif hook_event_name == 'afterShellExecution':
            shell_entry = {
                'type': hook_event_name,
                'command': event.get('command'),
                'output': event.get('output', '')
            }
            cwd = event.get('cwd') or next(iter(event.get('workspace_roots') or []), None)
            account = _destination_account(event.get('command'), cwd, account_state)
            if account:
                shell_entry['destination_account'] = account
            assistant_tool_uses.append(shell_entry)
        
        elif hook_event_name == 'afterMCPExecution':
            assistant_tool_uses.append({
                'type': hook_event_name,
                'tool_name': event.get('tool_name'),
                'tool_input': event.get('tool_input'),
                'result_json': event.get('result_json')
            })
        
        elif hook_event_name == 'afterAgentResponse':
            assistant_response = event.get('text')
    
    if user_prompt:
        messages.append({'role': 'user', 'content': user_prompt})
    
    if assistant_response:
        assistant_msg = {'role': 'assistant', 'content': assistant_response}
        if assistant_tool_uses:
            assistant_msg['tool_use'] = assistant_tool_uses
        messages.append(assistant_msg)
    
    if not messages:
        return None
    
    if not model or model == 'default':
        model = 'auto'

    exchange = {
        'conversation_id': conversation_id,
        'model': model,
        'messages': messages
    }
    
    return exchange


def send_to_api(exchange, api_key):
    """Send exchange data to Unbound API."""
    if not api_key:
        log_error("No API key present in send_to_api function")
        return False
    
    try:
        url = f"{UNBOUND_GATEWAY_URL}/v1/hooks/cursor"
        data = json.dumps(exchange)
        
        result = subprocess.run(
            ["curl", "-fsSL", "-X", "POST", "-H", f"Authorization: Bearer {api_key}",
             "-H", "Content-Type: application/json", "-d", data, url],
            capture_output=True,
            timeout=10
        )
        
        if result.returncode != 0:
            error_msg = result.stderr.decode('utf-8', errors='ignore').strip() if result.stderr else "Unknown error"
            log_error(f"API request failed: {error_msg}")
            return False
        return True
    except Exception as e:
        log_error(f"Exception in send_to_api: {str(e)}")
        return False


def cleanup_interrupted_requests(logs, current_conversation_id, current_generation_id):
    """
    Remove incomplete generation logs when a new generation starts in the same conversation.
    This handles interrupted requests (user stopped and started a new request).
    """
    cleaned_logs = []
    conversation_generations = defaultdict(set)
    
    # First pass: identify all generation_ids per conversation
    for log in logs:
        event = log.get('event', {})
        conv_id = event.get('conversation_id')
        gen_id = event.get('generation_id')
        if conv_id and gen_id:
            conversation_generations[conv_id].add(gen_id)
    
    # Check if current generation is new in this conversation
    if current_conversation_id in conversation_generations:
        existing_gens = conversation_generations[current_conversation_id]
        
        # If this is a new generation in the same conversation, remove incomplete ones
        if current_generation_id not in existing_gens:
            # Find incomplete generations (no stop event)
            for log in logs:
                event = log.get('event', {})
                conv_id = event.get('conversation_id')
                gen_id = event.get('generation_id')
                
                # Keep logs from other conversations or completed generations
                if conv_id != current_conversation_id:
                    cleaned_logs.append(log)
                elif conv_id == current_conversation_id and gen_id in existing_gens:
                    # Check if this generation has a stop event
                    has_stop = any(
                        l.get('event', {}).get('generation_id') == gen_id and
                        l.get('event', {}).get('hook_event_name') == 'stop'
                        for l in logs
                    )
                    if has_stop:
                        cleaned_logs.append(log)
                    # else: skip incomplete generation logs
            
            return cleaned_logs
    
    return logs


LOG_RETENTION_DAYS = 1


def cleanup_old_logs():
    """
    Manage log file size by removing old generation_ids when log count exceeds 50.
    Keeps only the most recent generation_id's entries to ensure current request is safe.
    Also prunes entries older than LOG_RETENTION_DAYS on a rolling basis.
    """
    logs = load_existing_logs()
    changed = False

    # Phase 1: Prune entries older than retention period
    cutoff = (datetime.now().astimezone() - timedelta(days=LOG_RETENTION_DAYS)).isoformat()
    fresh_logs = [log for log in logs if log.get('timestamp', '') >= cutoff]
    if len(fresh_logs) < len(logs):
        logs = fresh_logs
        changed = True

    # Phase 2: If still > 50 entries, keep only the most recent generation_id
    if len(logs) > 50:
        generation_order = []
        seen_generations = set()

        for log in logs:
            event = log.get('event', {})
            gen_id = event.get('generation_id')

            if gen_id and gen_id not in seen_generations:
                generation_order.append(gen_id)
                seen_generations.add(gen_id)

        if len(generation_order) > 1:
            most_recent_gen_id = generation_order[-1]
            logs = [
                log for log in logs
                if log.get('event', {}).get('generation_id') == most_recent_gen_id
            ]
            changed = True

    if changed:
        save_logs(logs)

    # Phase 3: Trim error.log to entries within retention period
    _trim_error_log(cutoff)


def _trim_error_log(cutoff_iso):
    """Remove error.log lines whose ISO-8601 timestamp prefix is older than cutoff."""
    try:
        if not ERROR_LOG.exists():
            return
        with open(ERROR_LOG, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        kept = [line for line in lines if line[:25] >= cutoff_iso[:25]]
        if len(kept) < len(lines):
            with open(ERROR_LOG, 'w', encoding='utf-8') as f:
                f.writelines(kept)
    except Exception:
        pass


def process_stop_event(generation_id, api_key=None):
    """Process stop event: convert to LLM format and send to API."""
    logs = load_existing_logs()

    # Group events
    grouped = group_events_by_generation(logs)

    # Find and process the generation with stop event
    for conversation_id, generations in grouped.items():
        if generation_id in generations:
            events = generations[generation_id]

            # Check if this generation has a stop event
            has_stop = any(
                log.get('event', {}).get('hook_event_name') == 'stop'
                for log in events
            )

            if has_stop:
                # Build LLM exchange
                exchange = build_llm_exchange(events, api_key)

                if exchange:
                    # Send to API
                    send_to_api(exchange, api_key)

                # Remove this generation's logs from agent-audit.log
                remaining_logs = [
                    log for log in logs
                    if log.get('event', {}).get('generation_id') != generation_id
                ]

                save_logs(remaining_logs)
                break


def main():
    """Main entry point - read from stdin and process events."""
    # Get API key (will be None if not set)
    api_key = os.getenv('UNBOUND_CURSOR_API_KEY')
    
    try:
        # Read JSON from stdin
        input_data = sys.stdin.read().strip()
        
        if not input_data:
            print("{}")
            return
        
        # Parse the event
        try:
            event = json.loads(input_data)
        except json.JSONDecodeError:
            print("{}")
            return

        # Get event details
        hook_event_name = event.get('hook_event_name')
        generation_id = event.get('generation_id')
        conversation_id = event.get('conversation_id')

        # Handle preToolUse - return immediately after decision is made
        if hook_event_name == 'preToolUse':
            response = process_pre_tool_use(event, api_key)
            print(json.dumps(response), flush=True)

            # Exit with code 2 to block the action if denied
            if response.get('decision') == 'deny':
                sys.exit(2)
            return

        # Handle beforeSubmitPrompt - check policy before processing
        if hook_event_name == 'beforeSubmitPrompt':
            response = process_user_prompt_submit(event, api_key)

            # If denied, transform response for Cursor format and exit
            if response.get('decision') == 'deny':
                cursor_response = {
                    'continue': False,
                    'user_message': response.get('reason', 'Prompt blocked by policy')
                }
                print(json.dumps(cursor_response), flush=True)
                sys.exit(2)

        # Create log entry with timestamp
        timestamp = datetime.now().astimezone().isoformat().replace('+00:00', 'Z')
        log_entry = {
            'timestamp': timestamp,
            'event': event
        }

        # Append to audit log
        append_to_audit_log(log_entry)

        # Handle interrupted requests (new generation in same conversation)
        if hook_event_name == 'beforeSubmitPrompt' and conversation_id and generation_id:
            logs = load_existing_logs()
            cleaned_logs = cleanup_interrupted_requests(logs, conversation_id, generation_id)
            if len(cleaned_logs) < len(logs):
                save_logs(cleaned_logs)
        
        # Process stop event
        if hook_event_name == 'stop' and generation_id:
            process_stop_event(generation_id, api_key)
            # Only cleanup after processing stop event to avoid race conditions
            cleanup_old_logs()
        
        # Output required by Cursor hooks
        print("{}")
        
    except Exception as e:
        # Log errors but still output {} to not break Cursor
        log_error(f"Exception in main: {str(e)}")
        print("{}", file=sys.stderr)
        print(f"Error: {e}", file=sys.stderr)
        print("{}")


if __name__ == '__main__':
    main()