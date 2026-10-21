#!/usr/bin/env python3
"""
Real-time Cursor hook event processor with smart garbage collection.
Reads JSON events from stdin, appends to agent-audit.log, and processes them on stop events.
"""

import sys
import json
import os
import re
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
    ('git', 'remote', 'get-url'),
    ('git', 'rev-parse', '--symbolic-full-name'),
    ('git', 'symbolic-ref', '--short', '-q', 'HEAD'),
    ('git', 'config', '--get'),
    ('git', 'config', '--get-regexp'),
    ('gh', 'config', 'get', '-h'),
    ('ssh', '-G'),
)
_ACCOUNT_RUN_TIMEOUT = 5
# Wall-clock budget for all lookups in one turn, so a slow tool never delays the agent.
_ACCOUNT_TURN_BUDGET = 10.0
# Wrappers that run the command after them: (short options that take a value, long ones that do, operands before it).
_ACCOUNT_WRAPPERS = {
    'sudo': ('ugCDhprtTU', {'--user', '--group', '--close-from', '--chdir', '--host', '--prompt', '--role', '--type',
                            '--command-timeout', '--other-user'}, 0),
    'doas': ('uC', set(), 0),
    'env': ('uCS', {'--unset', '--chdir', '--split-string'}, 0),
    'nice': ('n', {'--adjustment'}, 0),
    'timeout': ('ks', {'--kill-after', '--signal'}, 1),
    'time': ('fo', {'--format', '--output'}, 0),
    'stdbuf': ('ioe', {'--input', '--output', '--error'}, 0),
    'ionice': ('cnp', {'--class', '--classdata', '--pid'}, 0),
    'exec': ('a', set(), 0),
    'command': ('', set(), 0),
    'nohup': ('', set(), 0),
}
_ACCOUNT_SSH_TOOLS = frozenset({'ssh', 'scp', 'sftp', 'rsync'})
# Options that take a value, per program; every other option is a switch.
_ACCOUNT_SSH_VALUE_FLAGS = {'ssh': frozenset('BbcDEeFIiJLlmOoPpQRSWw'), 'scp': frozenset('cDFiJloPSX'),
                            'sftp': frozenset('BbcDFiJloPRSsX'), 'rsync': frozenset('efTMB@')}
# Of ssh's -o settings, only these change which host or user ssh -G resolves.
_ACCOUNT_SSH_IDENTITY_OPTIONS = frozenset({'hostname', 'user', 'port', 'proxyjump'})
_ACCOUNT_SSH_CONFIG_MAX_BYTES = 256 * 1024
_ACCOUNT_SSH_CONFIG_MAX_FILES = 32
_ACCOUNT_RSYNC_VALUE_OPTIONS = frozenset({
    '--exclude', '--include', '--filter', '--exclude-from', '--include-from', '--files-from', '--rsync-path',
    '--log-file', '--partial-dir', '--temp-dir', '--compare-dest', '--copy-dest', '--link-dest', '--backup-dir',
    '--chmod', '--chown', '--suffix', '--password-file', '--port', '--bwlimit', '--timeout', '--max-size',
    '--min-size', '--out-format', '--remote-option', '--block-size', '--modify-window'})
# gh commands that reach no repository.
_ACCOUNT_GH_NO_REPO = frozenset({'auth', 'help', 'version', 'config', 'alias', 'extension', 'completion',
                                 '--help', '-h', '--version'})
# gh commands that reach GitHub but not the checkout's repository, so only the host and login are known.
_ACCOUNT_GH_NO_CHECKOUT = frozenset({'search', 'gist', 'org', 'codespace', 'cs', 'ssh-key', 'gpg-key', 'status',
                                     'project'})
# gh commands whose -o names an organization, as --org does everywhere.
_ACCOUNT_GH_ORG_SHORT = frozenset({'secret', 'variable', 'ruleset'})
# gh options whose value is never a repository, skipped so a value such as --title '-Rx/y' is not read as one.
_ACCOUNT_GH_VALUE_OPTIONS = frozenset({
    '-t', '--title', '-b', '--body', '-F', '--body-file', '-B', '--base', '-H', '--head', '-l', '--label', '-a',
    '--assignee', '-r', '--reviewer', '-m', '--milestone', '--project', '-q', '--jq', '-T', '--template', '--json',
    '-S', '--search', '-s', '--state', '-L', '--limit', '-A', '--author', '-f', '--field', '--raw-field', '--header',
    '-X', '--method', '--input', '--workflow', '--ref', '-n', '--notes', '--notes-file', '--target', '-j', '--job',
    '--branch', '--message', '--name', '--description', '--visibility', '--color', '--env', '--key'})
# gh picks among unresolved remotes in this order.
_ACCOUNT_GH_REMOTE_ORDER = ('upstream', 'github', 'origin')
# Settings that move the repository or rewrite its config, so the remote read here may not be the one pushed to.
_ACCOUNT_GIT_OVERRIDE_ENV = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_GLOBAL',
                             'GIT_CONFIG_SYSTEM')
_ACCOUNT_GH_TOKEN_ENV = ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN')
# Reserved words that lead a command without being its program.
_ACCOUNT_SHELL_KEYWORDS = frozenset({'{', '}', 'if', 'then', 'else', 'elif', 'fi', 'do', 'done', 'while', 'until'})
# Set in a command's env by `env -i`: nothing is inherited from the hook's environment.
_ACCOUNT_CLEAN_ENV = '\0clean'


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


def _account_read_word(text: str, i: int) -> tuple:
    """(the shell word starting at i after blanks, the index after it); quotes and backslashes are undone."""
    while i < len(text) and text[i] in ' \t':
        i += 1
    word, quote = '', None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
            elif ch == '\\' and quote == '"' and i + 1 < len(text):
                i += 1
                word += text[i]
            else:
                word += ch
        elif ch in '\'"':
            quote = ch
        elif ch == '\\' and i + 1 < len(text):
            i += 1
            word += text[i]
        elif ch in ' \t\r\n;&|<>':
            break
        else:
            word += ch
        i += 1
    return word, i


def _account_segments(command: str) -> List[tuple]:
    """The command as (argv, operator before it) per simple command, and (None, '(' or ')') around each subshell;
    redirections and heredoc bodies are left out."""
    segments, argv, word, in_word, operator, quote, heredocs, i = [], [], '', False, '', None, [], 0
    while i < len(command):
        ch = command[i]
        if quote:
            if ch == quote:
                quote = None
            elif ch == '\\' and quote == '"' and i + 1 < len(command):
                i += 1
                word += command[i]
            else:
                word += ch
            i += 1
            continue
        if ch in '\'"':
            quote, in_word = ch, True
        elif ch == '\\' and command[i + 1:i + 2] == '\n':
            i += 1  # a backslash-newline continues the line
        elif ch == '\\' and i + 1 < len(command):
            i += 1
            word, in_word = word + command[i], True
        elif ch == '#' and not in_word:
            while i + 1 < len(command) and command[i + 1] != '\n':
                i += 1  # a comment runs to the end of its line
        elif ch in '<>' and command[i + 1:i + 2] == '(':
            # A process substitution runs its commands in a subshell, like $( ... ).
            if in_word:
                argv.append(word)
            word, in_word = '', False
        elif ch in '()':
            # Commands inside keep the operator before the subshell; its cd and exports end at the close.
            if in_word:
                argv.append(word)
            word, in_word = '', False
            if argv:
                segments.append((argv, operator))
            argv = []
            segments.append((None, ch))
            if ch == ')':
                operator = ';'
        elif ch in '<>' or command[i:i + 2] == '&>':
            # A redirection's operator, an fd number written against it, and its target are not arguments.
            if in_word and not (word.isdigit() and ch != '&'):
                argv.append(word)
            word, in_word = '', False
            op = re.match(r'&>>?|<<<|<<-|<<|<>|<&|>>|>&|>\||<|>', command[i:]).group(0)
            target, i = _account_read_word(command, i + len(op))
            if op in ('<<', '<<-'):
                heredocs.append((target, op == '<<-'))
            continue
        elif ch in ';&|\n':
            if in_word:
                argv.append(word)
            word, in_word = '', False
            pair = command[i:i + 2]
            following = pair if pair in ('&&', '||') else ';'
            i += 1 if following != ';' else 0
            if argv:
                segments.append((argv, operator))
                operator = following
            elif following != ';':
                operator = following
            argv = []
            # A heredoc's body starts on the next line and is input, not commands.
            while ch == '\n' and heredocs:
                delimiter, strip_tabs = heredocs.pop(0)
                while i + 1 < len(command):
                    end = command.find('\n', i + 1)
                    line = command[i + 1:] if end < 0 else command[i + 1:end]
                    i = len(command) - 1 if end < 0 else end
                    if (line.lstrip('\t') if strip_tabs else line) == delimiter:
                        break
        elif ch in ' \t\r':
            if in_word:
                argv.append(word)
            word, in_word = '', False
        else:
            word, in_word = word + ch, True
        i += 1
    if quote:
        return []
    if in_word:
        argv.append(word)
    if argv:
        segments.append((argv, operator))
    return segments


def _account_env(env: Dict, key: str) -> str:
    """A variable as the command sees it: set or unset in the command, else inherited from the hook's environment."""
    if key in env:
        return env[key]
    return '' if _ACCOUNT_CLEAN_ENV in env else os.environ.get(key, '')


def _account_is_assignment(token: str) -> bool:
    return '=' in token and not token.startswith('-') and token.split('=', 1)[0].replace('_', '').isalnum()


def _account_program(argv: List[str], cwd: Optional[str]) -> tuple:
    """(program, its args, inline VAR=value settings, folder it runs in, whether it runs as another user)."""
    env, argv, i, wrapper, operands, options_done, other_user = {}, list(argv), 0, None, 0, False, False
    while i < len(argv):
        token = argv[i]
        i += 1
        if _account_is_assignment(token) and (wrapper is None or options_done or wrapper in ('env', 'sudo')):
            key, value = token.split('=', 1)
            env[key] = value
            continue
        if wrapper is not None and not options_done and token == '--':
            options_done = True
            continue
        if wrapper == 'env' and not options_done and token in ('-', '--ignore-environment'):
            env.clear()
            env[_ACCOUNT_CLEAN_ENV] = ''
            continue
        if wrapper is not None and not options_done and token.startswith('-') and token != '-':
            short_flags, long_flags, _ = _ACCOUNT_WRAPPERS[wrapper]
            if wrapper == 'command' and token in ('-v', '-V'):
                return None, [], env, cwd, other_user  # only looks the command up
            name, value = token, None
            if token.startswith('--'):
                name, eq, attached = token.partition('=')
                if eq:
                    value = attached
                elif name in long_flags and i < len(argv):
                    value, i = argv[i], i + 1
            else:
                for j, flag in enumerate(token[1:], start=2):
                    if wrapper == 'env' and flag == 'i':
                        env.clear()
                        env[_ACCOUNT_CLEAN_ENV] = ''
                    if flag in short_flags:
                        name, value = '-' + flag, token[j:]
                        if not value and i < len(argv):
                            value, i = argv[i], i + 1
                        break
            if value is not None and name in {'env': ('-C', '--chdir'), 'sudo': ('-D', '--chdir')}.get(wrapper, ()):
                path = os.path.expanduser(value)
                cwd = path if os.path.isabs(path) else os.path.normpath(os.path.join(cwd or '', path))
            elif value is not None and name in ('-S', '--split-string') and wrapper == 'env':
                argv[i:i] = [w for segment, _ in _account_segments(value) if segment for w in segment]
            elif value is not None and name in ('-u', '--unset') and wrapper == 'env':
                env[value] = ''
            continue
        if wrapper is not None and operands > 0:
            operands -= 1
            continue
        if os.path.basename(token) in _ACCOUNT_WRAPPERS:
            wrapper, options_done = os.path.basename(token), False
            operands = _ACCOUNT_WRAPPERS[wrapper][2]
            other_user = other_user or wrapper in ('sudo', 'doas')
            continue
        return os.path.basename(token), argv[i:], env, cwd, other_user
    return None, [], env, cwd, other_user


def _account_strip_login(url: str) -> str:
    """A remote URL with any login removed: everything up to the last @ before the path, with or without a scheme."""
    return re.sub(r'^([a-zA-Z][a-zA-Z0-9+.-]*://)?[^/]*@', r'\1', url.strip())


def _account_host(value: str) -> str:
    """The host a host setting names, with any scheme, login, port or path dropped; empty when it names none."""
    authority = re.sub(r'^[a-z][a-z0-9+.-]*://', '', value.strip().lower()).split('/')[0].rsplit('@', 1)[-1]
    return authority.split(':')[0] if re.match(r'^[a-z0-9.-]+(:\d*)?$', authority) else ''


def _account_looks_like_url(value: str) -> bool:
    """Whether a git remote or target names a URL rather than a remote."""
    return bool(re.search(r'://|^[^/:]+@[^/:]+:|^[^/:]+:[^/]', value))


def _account_remote_parts(url: str) -> tuple:
    """(host, owner, repo) from an https or scp-style git remote; empty strings for what it doesn't name."""
    url = _account_strip_login(url)
    match = re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://([^/:]+)(?::\d+)?/(.*)$', url)
    if not match:
        match = re.match(r'^(?:[^@/:]+@)?([^/:]+):(?!//)(.*)$', url)
    if not match:
        return '', '', ''
    host, path = match.group(1).lower(), match.group(2).strip('/')
    if '@' in host:
        return '', '', ''
    if path.endswith('.git'):
        path = path[:-4]
    parts = [p for p in path.split('/') if p]
    owner = parts[0] if parts and '@' not in parts[0] and ':' not in parts[0] else ''
    repo = parts[-1] if len(parts) > 1 else ''
    return host, owner, repo


def _account_platform_for_host(host: str) -> str:
    if host == 'github.com' or host.endswith('.ghe.com'):
        return 'github'
    if host == 'gitlab.com':
        return 'gitlab'
    return 'git'


def _account_gh_user(host: str, env: Dict, cwd: Optional[str], state: Dict) -> str:
    """The gh login for a host, read from gh's local config; empty when a token or another config overrides it."""
    if any(_account_env(env, k) for k in _ACCOUNT_GH_TOKEN_ENV) or env.get('GH_CONFIG_DIR'):
        return ''
    key = ('gh-user', host)
    if key not in state:
        out = _account_run(['gh', 'config', 'get', '-h', host, 'user'], cwd, state)
        state[key] = (out or '').strip().splitlines()[0] if out and out.strip() else ''
    return state[key]


def _account_git_remotes(git_dir: str, state: Dict) -> Dict:
    """The repo's remotes by name, each as its remote.<name>.* settings; read once per folder and turn."""
    key = ('remotes', git_dir)
    if key not in state:
        out = _account_run(['git', '-C', git_dir, 'config', '--get-regexp', r'^remote\.'], git_dir, state) or ''
        remotes = {}
        for line in out.splitlines():
            name, _, value = line.partition(' ')
            match = re.match(r'^remote\.(.+)\.([a-z-]+)$', name)
            if match:
                remotes.setdefault(match.group(1), {})[match.group(2)] = value.strip()
        state[key] = remotes
    return state[key]


def _account_git_rewrite(url: str, base: List[str], git_dir: Optional[str], state: Dict) -> str:
    """An explicit push URL after git's url.<base>.pushInsteadOf and insteadOf rules, longest match first."""
    out = _account_run(base + ['config', '--get-regexp', r'^url\..*\.(pushinsteadof|insteadof)$'], git_dir, state)
    rules = []
    for line in (out or '').splitlines():
        name, _, prefix = line.partition(' ')
        match = re.match(r'^url\.(.*)\.(pushinsteadof|insteadof)$', name, re.IGNORECASE)
        if match and prefix:
            rules.append((match.group(2).lower(), prefix.strip(), match.group(1)))
    for kind in ('pushinsteadof', 'insteadof'):
        matches = [(len(prefix), prefix, replacement) for k, prefix, replacement in rules
                   if k == kind and url.startswith(prefix)]
        if matches:
            _, prefix, replacement = max(matches)
            return replacement + url[len(prefix):]
    return url


def _account_default_remote(base: List[str], git_dir: str, state: Dict) -> str:
    """The remote a bare git push uses: the one its branch pushes to, else pushRemote, pushDefault, the branch's remote, origin."""
    out = (_account_run(base + ['rev-parse', '--symbolic-full-name', '@{push}'], git_dir, state) or '').strip()
    if out.startswith('refs/remotes/'):
        # Remote names may hold a slash, so the longest configured name that prefixes the ref wins.
        names = [name for name in _account_git_remotes(git_dir, state) if out.startswith(f'refs/remotes/{name}/')]
        if names:
            return max(names, key=len)
    branch = (_account_run(base + ['symbolic-ref', '--short', '-q', 'HEAD'], git_dir, state) or '').strip()
    keys = ([f'branch.{branch}.pushRemote', 'remote.pushDefault', f'branch.{branch}.remote']
            if branch else ['remote.pushDefault'])
    for key in keys:
        value = (_account_run(base + ['config', '--get', key], git_dir, state) or '').strip()
        if value:
            return value
    return 'origin'


def _account_git_push(args: List[str], env: Dict, cwd: Optional[str], state: Dict) -> Optional[Dict]:
    """The account a `git push` reaches: the push remote's host and owner, and the gh login pushing over https."""
    git_dir, rest = cwd, list(args)
    overridden = any(env.get(k) for k in _ACCOUNT_GIT_OVERRIDE_ENV)
    while rest and rest[0].startswith('-'):
        flag = rest.pop(0)
        if flag == '-C' and rest:
            path = os.path.expanduser(rest.pop(0))
            git_dir = path if os.path.isabs(path) else (os.path.join(git_dir, path) if git_dir else None)
        elif flag in ('-c', '--git-dir', '--work-tree') and rest:
            rest.pop(0)
            overridden = True
        elif flag.startswith(('--git-dir=', '--work-tree=', '--config-env')) or (flag.startswith('-c') and len(flag) > 2):
            overridden = True
        elif flag == '--namespace' and rest:
            rest.pop(0)
    if not rest or rest[0] != 'push':
        return None
    if overridden:
        # Unknown is safe; the cwd repo's remote would be a confident wrong answer.
        return {'platform': 'git', 'source': 'unknown'}
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
    remote = ''
    if not (target and _account_looks_like_url(target)) and not git_dir:
        # Without the folder the push ran in, its remotes can't be read.
        return {'platform': 'git', 'source': 'unknown'}
    if target and _account_looks_like_url(target):
        url = _account_git_rewrite(target, base, git_dir, state)
    else:
        remote = target or _account_default_remote(base, git_dir, state)
        # pushDefault and a branch's remote may name a URL instead of a remote.
        url = _account_git_rewrite(remote, base, git_dir, state) if _account_looks_like_url(remote) else \
            _account_run(base + ['remote', 'get-url', '--push', remote], git_dir, state)
        remote = '' if _account_looks_like_url(remote) else remote
    host, owner, _repo = _account_remote_parts(url.strip().splitlines()[0]) if url else ('', '', '')
    if not host:
        return {'platform': 'git', 'source': 'unknown'}
    if git_dir:
        state['_push_dir'] = os.path.realpath(git_dir)
    user = ''
    # A remote that carries its own password never asks the credential helper.
    if re.match(r'^https?://', url.strip()) and not re.match(r'^[a-z]+://[^/@]*:[^/@]*@', url.strip(), re.IGNORECASE):
        helper = _account_run(base + ['config', '--get', f'credential.https://{host}.helper'], git_dir, state) or \
            _account_run(base + ['config', '--get', 'credential.helper'], git_dir, state) or ''
        if 'auth git-credential' in helper:
            user = _account_gh_user(host, env, git_dir, state)
    return {'platform': _account_platform_for_host(host), 'host': host, 'account': owner, 'user': user,
            'context': remote or '', 'source': 'command'}


def _account_gh_repo_remote(cwd: str, state: Dict) -> str:
    """The URL of the remote gh queries: the one `gh repo set-default` chose, else upstream, github, origin, the first."""
    remotes = _account_git_remotes(cwd, state)
    for name, settings in remotes.items():
        resolved = settings.get('gh-resolved', '')
        if resolved and resolved != 'base' and '/' in resolved:
            host = _account_remote_parts(settings.get('url', ''))[0] or 'github.com'
            return f'https://{host}/{resolved}'
        if resolved == 'base':
            return settings.get('url', '')
    for name in _ACCOUNT_GH_REMOTE_ORDER + tuple(sorted(remotes)):
        if remotes.get(name, {}).get('url'):
            return remotes[name]['url']
    return ''


def _account_gh(args: List[str], env: Dict, cwd: Optional[str], state: Dict) -> Optional[Dict]:
    """The account a gh command reaches: host and owner from -R, a repo argument, GH_REPO or the repo's remote, plus the gh login."""
    if not args or args[0] in _ACCOUNT_GH_NO_REPO:
        return None
    repo, flag_host, org, positional, i, checkout_bound = '', '', '', [], 0, True
    while i < len(args):
        arg = args[i]
        i += 1
        if arg in ('-R', '--repo') and i < len(args):
            repo, i = args[i], i + 1
        elif arg.startswith('--repo='):
            repo = arg.split('=', 1)[1]
        elif arg.startswith('-R') and not arg.startswith('--'):
            repo = arg[2:].lstrip('=')
        elif arg == '--hostname' and i < len(args):
            flag_host, i = _account_host(args[i]), i + 1
        elif arg.startswith('--hostname='):
            flag_host = _account_host(arg.split('=', 1)[1])
        elif arg == '--org' and i < len(args):
            org, i = args[i], i + 1
        elif arg.startswith('--org='):
            org = arg.split('=', 1)[1]
        elif arg.startswith('-o') and positional[:1] and positional[0] in _ACCOUNT_GH_ORG_SHORT:
            org = arg[2:].lstrip('=')
            if not org and i < len(args):
                org, i = args[i], i + 1
        elif arg in _ACCOUNT_GH_VALUE_OPTIONS:
            i += 1
        elif not arg.startswith('-'):
            positional.append(arg)
    # `gh repo view|clone|fork|… OWNER/REPO`, a pr or issue URL, and `gh api repos/OWNER/REPO/…` name the repository.
    if not repo and len(positional) > 2 and positional[0] == 'repo' and '/' in positional[2]:
        repo = positional[2]
    if not repo and positional[:1] != ['api']:
        repo = next((m.group(1) for m in (re.match(r'^(https?://[^/@]+/[^/@]+/[^/@]+)', _account_strip_login(a))
                                           for a in positional[1:]) if m), '')
    if positional[:1] and positional[0] in _ACCOUNT_GH_NO_CHECKOUT or \
            positional[:2] in (['repo', 'list'], ['repo', 'create']) and not repo:
        cwd = None
        checkout_bound = False
    if not repo and positional[:1] == ['api']:
        url = next((_account_strip_login(a) for a in positional[1:] if re.match(r'^https?://', a)), '')
        # A full URL names the host gh requests, whatever --hostname says.
        flag_host = _account_host(url) if url else flag_host
        named = re.match(r'^https?://[^/]+/(?:api/v3/)?repos/([^/{}@]+/[^/{}@]+)', url) or next(
            (m for m in (re.match(r'^/?repos/([^/{}@]+/[^/{}@]+)', a) for a in positional[1:]) if m), None)
        if named:
            repo = named.group(1)
        else:
            # An api path that names no repo reaches no repo of this checkout.
            checkout_bound = any('{owner}' in a or '{repo}' in a for a in positional)
            cwd = cwd if checkout_bound else None
    if org:
        # An organization's secrets, variables and rulesets are not the checkout's.
        repo, cwd, checkout_bound = org, None, False
    # GH_REPO only stands in for a command that works on a repository.
    repo = repo or ('' if cwd is None and not checkout_bound else _account_env(env, 'GH_REPO'))
    env_host = _account_host(_account_env(env, 'GH_HOST'))
    host, owner = '', ''
    if repo:
        repo = _account_strip_login(repo)  # never keep a login or token
        if ':' in repo:
            host, owner, _repo = _account_remote_parts(repo)
        else:
            parts = [p for p in repo.split('/') if p]
            host, owner = (parts[0].lower(), parts[1]) if len(parts) >= 3 else ('', parts[0] if parts else '')
        if '@' in host or '@' in owner:
            host, owner = '', ''
    elif cwd:
        url = _account_gh_repo_remote(cwd, state)
        if url:
            host, owner, _repo = _account_remote_parts(url.strip().splitlines()[0])
            state['_push_dir'] = os.path.realpath(cwd)
    host = flag_host or host or env_host or 'github.com'
    return {'platform': 'github', 'host': host, 'account': owner, 'user': _account_gh_user(host, env, cwd, state),
            'context': '', 'source': 'command'}


def _account_remote_operand(operands: List[str]) -> tuple:
    """(the last remote [user@]host operand, its port) for a copy; (None, None) for an rsync daemon or local paths."""
    for arg in reversed(operands):
        uri = re.match(r'^(scp|sftp)://(?:([^@/;:]+)(?:[;:][^@/]*)?@)?(\[[^\]/]+\]|[^/:\[\]@]+)(?::(\d+))?(?=/|$)', arg)
        if uri:
            # A scp:// or sftp:// URI names the login and port itself; anything after the user (a password) is dropped.
            return (f'{uri.group(2)}@{uri.group(3)}' if uri.group(2) else uri.group(3)), uri.group(4)
        if '://' in arg:
            return None, None
        # A bracketed host may hold an IPv6 address; a second colon after the host marks an rsync daemon.
        match = re.match(r'^((?:[^@/:\[\]]+@)?(?:\[[^\]/]+\]|[^/:\[\]]+)):(:)?', arg)
        # A drive letter is a local path only on Windows; elsewhere scp dials host C.
        if match and not (os.name == 'nt' and re.match(r'^[A-Za-z]:[\\/]', arg)):
            return (None, None) if match.group(2) else (match.group(1), None)
    return None, None


def _account_rsync_destination(args: List[str], env: Dict) -> tuple:
    """(destination, ssh identity options) for rsync: the last remote operand, reached through its remote shell."""
    shell = _account_env(env, 'RSYNC_RSH')
    operands, i = [], 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == '--rsh' and i < len(args):
            shell, i = args[i], i + 1
        elif arg.startswith('--rsh='):
            shell = arg.split('=', 1)[1]
        elif arg in _ACCOUNT_RSYNC_VALUE_OPTIONS:
            i += 1
        elif arg.startswith('-') and not arg.startswith('--'):
            # Walk a cluster such as -avze: the first option that takes a value takes the rest of it, or the next word.
            for j, flag in enumerate(arg[1:], start=2):
                if flag in _ACCOUNT_SSH_VALUE_FLAGS['rsync']:
                    value = arg[j:]
                    if not value and i < len(args):
                        value, i = args[i], i + 1
                    if flag == 'e':
                        shell = value
                    break
        elif not arg.startswith('-'):
            operands.append(arg)
    passthrough = []
    if shell:
        shell_argv = [w for segment, _ in _account_segments(shell) if segment for w in segment]
        # A remote shell other than ssh never reads the ssh config.
        if not shell_argv or os.path.basename(shell_argv[0]) != 'ssh':
            return None, []
        passthrough = _account_ssh_destination('ssh', shell_argv[1:] + ['-'], env)[1]
    destination, port = _account_remote_operand(operands)
    if not destination:
        return None, []
    return destination, passthrough + (['-p', port] if port else [])


def _account_ssh_destination(program: str, args: List[str], env: Dict) -> tuple:
    """(destination, identity options to pass to ssh -G) for ssh, scp, sftp or rsync; (None, []) when there is none."""
    if program == 'rsync':
        return _account_rsync_destination(args, env)
    passthrough, operands, i = [], [], 0
    value_flags = _ACCOUNT_SSH_VALUE_FLAGS.get(program, frozenset())
    # Only the options that change which host or user ssh -G resolves are passed through; scp's -l is a bandwidth limit.
    identity_flags = ('l', 'o', 'F', 'J', 'p') if program == 'ssh' else ('o', 'F', 'J', 'P')
    while i < len(args):
        arg = args[i]
        i += 1
        # ssh reads options before and right after its destination; the first other word starts the remote command.
        reading_options = not operands or (program == 'ssh' and len(operands) == 1)
        if not reading_options or not arg.startswith('-') or arg == '-':
            operands.append(arg)
            continue
        if arg == '--':
            operands.extend(args[i:])
            break
        # Walk a cluster such as -vF: the first option that takes a value takes the rest of it, or the next word.
        for j, flag in enumerate(arg[1:], start=2):
            if flag not in value_flags:
                continue
            value = arg[j:]
            if not value and i < len(args):
                value, i = args[i], i + 1
            option = re.split(r'[\s=]', value.strip(), maxsplit=1)[0].lower()
            if value and flag in identity_flags and (flag != 'o' or option in _ACCOUNT_SSH_IDENTITY_OPTIONS):
                passthrough += ['-p' if flag == 'P' else '-' + flag, value]
            break
    if program == 'ssh':
        return (operands[0], passthrough) if operands else (None, [])
    destination, port = _account_remote_operand(operands)
    if destination:
        return destination, passthrough + (['-p', port] if port else [])
    # sftp also takes a plain [user@]host with no path.
    if program == 'sftp' and operands and re.match(r'^(?:[^@/:\[\]]+@)?(?:\[[^\]/]+\]|[^/:\[\]]+)$', operands[0]):
        return operands[0], passthrough
    return None, []


def _account_ssh_config_words(line: str) -> Optional[List[str]]:
    """An ssh config line split the way OpenSSH splits it; None for an unbalanced quote, which ssh rejects."""
    words, word, in_word, quote, i = [], '', False, None, 0
    while i < len(line):
        ch = line[i]
        # A backslash escapes a quote, a backslash, or (outside quotes) a space; any other backslash is kept.
        if ch == '\\' and i + 1 < len(line) and (line[i + 1] in '\'"\\' or (quote is None and line[i + 1] == ' ')):
            word, in_word, i = word + line[i + 1], True, i + 2
            continue
        if quote is not None:
            if ch == quote:
                quote = None
            else:
                word += ch
        elif ch in '\'"':
            quote, in_word = ch, True
        elif ch in ' \t':
            if in_word:
                words.append(word)
            word, in_word = '', False
        elif ch == '#' and not in_word:
            break
        else:
            word, in_word = word + ch, True
        i += 1
    if quote is not None:
        return None
    if in_word:
        words.append(word)
    return words


def _account_home() -> str:
    """The home folder ssh reads its config from: the account's own, which $HOME does not change."""
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    except Exception:
        return os.path.expanduser('~')


def _account_ssh_config_unsafe(passthrough: List[str], cwd: Optional[str]) -> bool:
    """Whether ssh -G could run a command or a DNS lookup while reading its configs, or reads a file this can't check."""
    import glob
    import stat
    home = _account_home()
    home_ssh = os.path.join(home, '.ssh')
    config = None
    for flag, value in zip(passthrough[::2], passthrough[1::2]):
        if flag == '-F':
            config = os.path.expanduser(value)
    if config == 'none':
        return False
    if config is None:
        pending = [(os.path.join(home_ssh, 'config'), home_ssh), ('/etc/ssh/ssh_config', '/etc/ssh')]
    elif os.path.isabs(config) or (cwd and os.path.isdir(cwd)):
        pending = [(os.path.join(cwd or '', config), home_ssh)]
    else:
        return True
    seen = set()
    while pending:
        path, base = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        try:
            info = os.stat(path)
        except FileNotFoundError:
            continue
        except OSError:
            return True
        # Only small regular files are read, so a fifo or a huge file can't hold up the turn.
        if len(seen) > _ACCOUNT_SSH_CONFIG_MAX_FILES or not stat.S_ISREG(info.st_mode) or \
                info.st_size > _ACCOUNT_SSH_CONFIG_MAX_BYTES:
            return True
        try:
            with open(path, errors='ignore') as handle:
                lines = handle.read().splitlines()
        except OSError:
            return True
        for line in lines:
            words = _account_ssh_config_words(line)
            if words is None:
                return True
            if not words:
                continue
            keyword, _, attached = words[0].partition('=')
            # ssh allows `Keyword=value` and `Keyword =value`, so a leading = is only a separator.
            words = [w.lstrip('=') for w in ([attached] if attached else []) + words[1:] if w.lstrip('=')]
            if keyword.lower() == 'match' and any(w.lower().lstrip('!').startswith('exec') for w in words):
                return True
            if keyword.lower() == 'canonicalizehostname' and words and words[0].lower() in ('yes', 'always'):
                return True
            for pattern in (words if keyword.lower() == 'include' else []):
                if '$' in pattern or '%' in pattern:
                    return True  # ssh may expand a variable or token here
                pattern = home + pattern[1:] if pattern == '~' or pattern.startswith('~/') else os.path.expanduser(pattern)
                # ssh globs the path, and glob reads a backslash as escaping the next character.
                pattern = re.sub(r'\\(.)', r'\1', pattern)
                pending += [(found, base) for found in sorted(glob.glob(os.path.join(base, pattern)))]
    return False


def _account_ssh(program: str, args: List[str], env: Dict, cwd: Optional[str], state: Dict,
                 other_user: bool = False) -> Optional[Dict]:
    """The host and user an ssh-family command reaches, resolved through ssh -G."""
    destination, passthrough = _account_ssh_destination(program, args, env)
    if not destination or destination.startswith('-'):
        return None  # a word starting with - would reach ssh -G as an option
    if other_user:
        # Under sudo, ssh runs as that user, with that user's config and default login.
        return {'platform': 'ssh', 'source': 'unknown'}
    if _account_ssh_config_unsafe(passthrough, cwd):
        return {'platform': 'ssh', 'source': 'unknown'}
    out = _account_run(['ssh', '-G'] + passthrough + [destination.replace('[', '').replace(']', '')], cwd, state)
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


def _account_rewrites_remotes(args: List[str], cwd: Optional[str]) -> tuple:
    """(whether a git command changes remote or URL settings, the folder whose remotes it changes or None for all)."""
    git_dir, rest = cwd, list(args)
    while rest and rest[0].startswith('-'):
        flag = rest.pop(0)
        if flag == '-C' and rest:
            path = os.path.expanduser(rest.pop(0))
            git_dir = path if os.path.isabs(path) else (os.path.join(git_dir, path) if git_dir else None)
        elif flag in ('-c', '--git-dir', '--work-tree', '--namespace') and rest:
            rest.pop(0)
    git_dir = os.path.realpath(git_dir) if git_dir else None
    if rest[:1] == ['remote'] and len(rest) > 1 and rest[1] in ('add', 'set-url', 'rename', 'remove', 'rm'):
        return True, git_dir
    if rest[:1] == ['config']:
        options = [a for a in rest[1:] if a.startswith('-')]
        keys = [a.lower() for a in rest[1:] if not a.startswith('-')]
        reads = {'--get', '--get-all', '--get-regexp', '--list', '-l', '--get-urlmatch'} & set(options) or \
            keys[:1] in (['get'], ['list'])
        # git 2.46 also takes `git config set|unset <key>`.
        keys = keys[1:] if keys[:1] in (['set'], ['unset']) else keys
        if not reads and keys and keys[0].startswith(('remote.', 'url.', 'branch.')):
            scoped = {'--global', '--system', '--file', '-f'} & set(options)
            return True, None if scoped else git_dir
    return False, git_dir


def _destination_account(command: Optional[str], cwd: Optional[str], state: Dict) -> Optional[Dict]:
    """The account the first resolved command in a shell call reached, else the first unresolved one, or None."""
    try:
        if not isinstance(command, str) or not command.strip():
            return None
        found, unresolved, exported, status, subshells = None, None, {}, None, []
        for argv, operator in _account_segments(command):
            if argv is None:
                # A subshell's cd and exports end with it.
                if operator == '(':
                    subshells.append((cwd, dict(exported)))
                elif subshells:
                    cwd, exported = subshells.pop()
                continue
            while argv and argv[0] in _ACCOUNT_SHELL_KEYWORDS:
                argv = argv[1:]
            if not argv:
                continue
            # A literal true or false decides whether && or || runs the next command; any other status is unknown.
            if (operator == '&&' and status == 1) or (operator == '||' and status == 0):
                continue
            program, args, env, seg_cwd, other_user = _account_program(argv, cwd)
            env = env if _ACCOUNT_CLEAN_ENV in env else {**exported, **env}
            status = 0 if program in ('true', ':') else 1 if program == 'false' else None
            if program == 'export':
                exported.update(a.split('=', 1) for a in args if _account_is_assignment(a))
                continue
            if program == 'unset':
                # Unset hides the inherited value too, so it is kept as empty rather than dropped.
                exported.update((name, '') for name in args if not name.startswith('-'))
                continue
            if program == 'cd':
                # Later segments run in the folder this one moves to.
                target = os.path.expanduser(args[0]) if args else os.path.expanduser('~')
                cwd = target if os.path.isabs(target) else os.path.normpath(os.path.join(seg_cwd or '', target))
                continue
            if program == 'git':
                rewrites, rewritten_dir = _account_rewrites_remotes(args, seg_cwd)
                if rewrites:
                    # A push already resolved against these remotes in this turn can no longer be trusted.
                    for result_dir, result in state.get('push_results', []):
                        if rewritten_dir is None or result_dir == rewritten_dir:
                            result.clear()
                            result.update({'platform': 'git', 'source': 'unknown'})
                    continue
            if found is not None:
                continue
            account = None
            state.pop('_push_dir', None)
            if program == 'git':
                account = _account_git_push(args, env, seg_cwd, state)
            elif program == 'gh':
                account = _account_gh(args, env, seg_cwd, state)
            elif program in _ACCOUNT_SSH_TOOLS:
                account = _account_ssh(program, args, env, seg_cwd, state, other_user)
            # The folder whose remotes this answer was read from, if it was read from a remote.
            push_dir = state.pop('_push_dir', None)
            if account:
                account = {k: v for k, v in account.items() if v or k in ('platform', 'source')}
                if account['source'] != 'unknown':
                    found = account
                    if push_dir is not None:
                        state.setdefault('push_results', []).append((push_dir, account))
                else:
                    unresolved = unresolved or account
        return found or unresolved
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
    shell_cwds = {}
    
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
        
        elif hook_event_name in ('shellCwd', 'beforeShellExecution'):
            if isinstance(event.get('cwd'), str) and event.get('cwd'):
                shell_cwds.setdefault(event.get('command'), set()).add(event.get('cwd'))

        elif hook_event_name == 'afterShellExecution':
            shell_entry = {
                'type': hook_event_name,
                'command': event.get('command'),
                'output': event.get('output', '')
            }
            noted = shell_cwds.get(event.get('command'), set())
            # Where the command ran: its own cwd, else the one noted before it ran, else the first workspace root.
            # One command noted in two folders this turn can't be matched to its run, so that run gets no account.
            cwd = event.get('cwd') or (next(iter(noted)) if len(noted) == 1 else None) or \
                next(iter(event.get('workspace_roots') or []), None)
            unmatched = not event.get('cwd') and len(noted) > 1
            account = None if unmatched else _destination_account(event.get('command'), cwd, account_state)
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


def _note_shell_cwd(event):
    """Log where a shell command starts, since Cursor's afterShellExecution doesn't say; never raises."""
    try:
        tool_input = event.get('tool_input') if isinstance(event.get('tool_input'), dict) else {}
        command = event.get('command') or tool_input.get('command')
        cwd = tool_input.get('working_directory') or event.get('cwd')
        if isinstance(command, str) and isinstance(cwd, str) and cwd:
            append_to_audit_log({
                'timestamp': datetime.now().astimezone().isoformat().replace('+00:00', 'Z'),
                'event': {'hook_event_name': 'shellCwd', 'conversation_id': event.get('conversation_id'),
                          'generation_id': event.get('generation_id'), 'command': command, 'cwd': cwd},
            })
    except Exception:
        pass


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
            _note_shell_cwd(event)
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