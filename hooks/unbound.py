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
from typing import Any, Dict, List, Optional


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


# KEEP IN SYNC: transfer resolution — byte-identical in every agent hook, here and in cursor-plugin, do not diverge.
# --- Transfer resolution: where each shell transfer went, as host and address kind only, fail-open ---

# Only these commands are ever run; everything else is refused. DNS is a resolver call, not a command.
_TRANSFER_ALLOWED_RUNS = (('ssh', '-G'),)
_TRANSFER_RUN_TIMEOUT = 5
_TRANSFER_DNS_TIMEOUT = 1.5
# Wall-clock budget for all lookups in one turn, so a slow lookup never delays the agent.
_TRANSFER_TURN_BUDGET = 10.0
_TRANSFER_MAX_ENDPOINTS = 10
# How deep substitutions and `sh -c` strings are followed.
_TRANSFER_MAX_DEPTH = 8
# How many function calls are followed in one command, so a recursive function stays cheap.
_TRANSFER_MAX_CALLS = 32
# How many words one brace expansion or curl URL glob is expanded into.
_TRANSFER_MAX_WORDS = 64
# Wrappers that run the command after them: (short options that take a value, long ones that do, operands before it).
_TRANSFER_WRAPPERS = {
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
    'builtin': ('', set(), 0),
    'nohup': ('', set(), 0),
}
# Reserved words that open, divide or close a compound command.
_TRANSFER_KEYWORDS = frozenset({'{', '}', 'if', 'then', 'elif', 'else', 'fi', 'while', 'until', 'for', 'select', 'do',
                                'done', '!', 'function'})
# Reserved words after which the next word still starts a command.
_TRANSFER_LEADING_WORDS = frozenset({'{', 'if', 'then', 'elif', 'else', 'while', 'until', 'do', '!'})
_TRANSFER_LOOPS = ('while', 'until', 'for', 'select')
_TRANSFER_DECLARES = ('declare', 'typeset', 'local', 'readonly')
_TRANSFER_SHELLS = frozenset({'sh', 'bash', 'zsh', 'dash', 'ksh', 'ash'})
_TRANSFER_POWERSHELLS = frozenset({'powershell', 'pwsh'})
# Set in a command's env by `env -i`: nothing is inherited from the hook's environment.
_TRANSFER_CLEAN_ENV = '\0clean'
# Stands in for a substitution's output inside a word, so no host is read from it.
_TRANSFER_SUBSTITUTED = '\0'
# Mark a word's unquoted { , and }, which bash may brace-expand, and turn them back once it has.
_TRANSFER_BRACE_MARKS = {'{': '\1', ',': '\2', '}': '\3'}
_TRANSFER_UNMARK = {1: '{', 2: ',', 3: '}'}
_TRANSFER_TOOLS = {
    'curl': 'curl', 'wget': 'wget', 'scp': 'scp', 'rsync': 'rsync', 'sftp': 'sftp', 'ssh': 'ssh',
    'nc': 'nc', 'ncat': 'nc', 'netcat': 'nc', 'socat': 'socat', 'http': 'httpie', 'https': 'httpie',
    'invoke-webrequest': 'powershell', 'invoke-restmethod': 'powershell', 'iwr': 'powershell', 'irm': 'powershell',
}
_TRANSFER_SENDING_METHODS = frozenset({'POST', 'PUT', 'PATCH'})
# The port a URL connects to when it names none, by scheme.
_TRANSFER_DEFAULT_PORTS = {'http': 80, 'https': 443, 'ftp': 21, 'ftps': 990, 'ws': 80, 'wss': 443}
# Options that take a value, per program; every other option is a switch.
_TRANSFER_SSH_VALUE_FLAGS = {'ssh': frozenset('BbcDEeFIiJLlmOoPpQRSWw'), 'scp': frozenset('cDFiJloPSX'),
                             'sftp': frozenset('BbcDFiJloPRSsX'), 'rsync': frozenset('efTMB@')}
# Of ssh's -o settings, only these change which host or user ssh -G resolves.
_TRANSFER_SSH_IDENTITY_OPTIONS = frozenset({'hostname', 'user', 'port', 'proxyjump'})
_TRANSFER_SSH_CONFIG_MAX_BYTES = 256 * 1024
_TRANSFER_SSH_CONFIG_MAX_FILES = 32
_TRANSFER_RSYNC_VALUE_OPTIONS = frozenset({
    '--exclude', '--include', '--filter', '--exclude-from', '--include-from', '--files-from', '--rsync-path',
    '--log-file', '--partial-dir', '--temp-dir', '--compare-dest', '--copy-dest', '--link-dest', '--backup-dir',
    '--chmod', '--chown', '--suffix', '--password-file', '--port', '--bwlimit', '--timeout', '--max-size',
    '--min-size', '--out-format', '--remote-option', '--block-size', '--modify-window'})
# curl's options that take a value (`curl --help all`), so a value is never read as a URL.
_TRANSFER_CURL_SHORT_VALUES = frozenset('ACDEFHKPQTUXYbcdemortuwxyz')
_TRANSFER_CURL_LONG_VALUES = frozenset((
    '--abstract-unix-socket --alt-svc --aws-sigv4 --cacert --capath --cert --cert-type --ciphers --config '
    '--connect-timeout --connect-to --continue-at --cookie --cookie-jar --create-file-mode --crlfile --curves --data '
    '--data-ascii --data-binary --data-raw --data-urlencode --delegation --dns-interface --dns-ipv4-addr '
    '--dns-ipv6-addr --dns-servers --doh-url --dump-header --egd-file --engine --etag-compare --etag-save '
    '--expect100-timeout --form --form-string --ftp-account --ftp-alternative-to-user --ftp-method --ftp-port '
    '--ftp-ssl-ccc-mode --happy-eyeballs-timeout-ms --haproxy-clientip --header --hostpubmd5 --hostpubsha256 --hsts '
    '--interface --ipfs-gateway --json --keepalive-time --key --key-type --krb --libcurl --limit-rate --local-port '
    '--login-options --mail-auth --mail-from --mail-rcpt --max-filesize --max-redirs --max-time --netrc-file '
    '--noproxy --oauth2-bearer --output --output-dir --parallel-max --pass --pinnedpubkey --preproxy --proto '
    '--proto-default --proto-redir --proxy --proxy-cacert --proxy-capath --proxy-cert --proxy-cert-type '
    '--proxy-ciphers --proxy-crlfile --proxy-header --proxy-key --proxy-key-type --proxy-pass --proxy-pinnedpubkey '
    '--proxy-service-name --proxy-tls13-ciphers --proxy-tlsauthtype --proxy-tlspassword --proxy-tlsuser --proxy-user '
    '--pubkey --quote --random-file --range --rate --referer --request --request-target --resolve --retry '
    '--retry-delay --retry-max-time --sasl-authzid --service-name --socks4 --socks4a --socks5 '
    '--socks5-gssapi-service --socks5-hostname --speed-limit --speed-time --stderr --telnet-option --tftp-blksize '
    '--time-cond --tls-max --tls13-ciphers --tlsauthtype --tlspassword --tlsuser --trace --trace-ascii '
    '--trace-config --unix-socket --upload-file --url --url-query --user --user-agent --variable --write-out').split())
_TRANSFER_CURL_UPLOADS = frozenset({'-T', '--upload-file', '-F', '--form', '--form-string', '--json'})
_TRANSFER_CURL_BODIES = frozenset({'-d', '--data', '--data-ascii', '--data-binary', '--data-raw', '--data-urlencode'})
_TRANSFER_WGET_SHORT_VALUES = frozenset('aABDeIilnOoPQRtTUwX')
_TRANSFER_WGET_LONG_VALUES = frozenset((
    '--output-file --append-output --execute --input-file --base --config --rejected-log --tries '
    '--retry-on-http-error --output-document --backups --timeout --dns-timeout --connect-timeout --read-timeout '
    '--wait --waitretry --bind-address --limit-rate --quota --user --password --header --compression --max-redirect '
    '--proxy-user --proxy-password --referer --save-cookies --load-cookies --post-data --post-file --method '
    '--body-data --body-file --default-page --directory-prefix --cut-dirs --level --accept --reject --accept-regex '
    '--reject-regex --regex-type --domains --exclude-domains --follow-tags --ignore-tags --include-directories '
    '--exclude-directories --user-agent --secure-protocol --certificate --certificate-type --private-key '
    '--private-key-type --ca-certificate --ca-directory --crl-file --pinnedpubkey --random-file --egd-file --ciphers '
    '--hsts-file --ftp-user --ftp-password --http-user --http-password --warc-file --warc-header --warc-max-size '
    '--warc-dedup --warc-tempdir --local-encoding --remote-encoding --restrict-file-names --prefer-family '
    '--report-speed --progress --use-askpass').split())
_TRANSFER_WGET_UPLOADS = frozenset({'--post-data', '--post-file', '--body-data', '--body-file'})
_TRANSFER_NC_SHORT_VALUES = frozenset('eGgIiMmOoPpqsTVWwXx')
_TRANSFER_NC_LONG_VALUES = frozenset((
    '--proxy --proxy-type --proxy-auth --proxy-dns --source --source-port --wait --idle-timeout --exec --sh-exec '
    '--lua-exec --output --hex-dump --allow --allowfile --deny --denyfile --max-conns --delay --ssl-cert --ssl-key '
    '--ssl-trustfile --ssl-ciphers --ssl-servername --ssl-alpn').split())
_TRANSFER_SOCAT_VALUE_OPTIONS = frozenset({'-b', '-t', '-T', '-L', '-W', '-lf', '-lp'})
_TRANSFER_HTTPIE_SHORT_VALUES = frozenset('aAoPps')
_TRANSFER_HTTPIE_LONG_VALUES = frozenset((
    '--auth --auth-type --session --session-read-only --output --print --history-print --pretty --style '
    '--format-options --response-charset --response-mime --verify --cert --cert-key --cert-key-pass --ssl --ciphers '
    '--timeout --max-redirects --max-headers --proxy --default-scheme --boundary --raw').split())
# Invoke-WebRequest and Invoke-RestMethod parameters that take no value.
_TRANSFER_PS_SWITCHES = frozenset({
    'usebasicparsing', 'usedefaultcredentials', 'proxyusedefaultcredentials', 'disablekeepalive', 'passthru',
    'skipcertificatecheck', 'skipheadervalidation', 'skiphttperrorcheck', 'allowunencryptedauthentication',
    'allowinsecureredirect', 'noproxy', 'preserveauthorizationonredirect', 'preservehttpmethodonredirect', 'resume',
    'followrellink', 'verbose', 'debug', 'whatif', 'confirm'})
# Address kinds, checked in order; any other address is public.
_TRANSFER_ADDRESS_KINDS = (
    ('loopback', ('127.0.0.0/8', '0.0.0.0/32', '::1/128', '::/128')),
    ('private', ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7')),
    ('link_local', ('169.254.0.0/16', 'fe80::/10')),
    ('cgnat', ('100.64.0.0/10',)),
)


def _transfer_deadline(state: Dict) -> float:
    """When this turn's lookups must stop; a caller may seed a shorter budget in state."""
    return state.setdefault('deadline', time.monotonic() + state.get('budget', _TRANSFER_TURN_BUDGET))


def _transfer_run(argv: List[str], cwd: Optional[str], state: Dict) -> Optional[str]:
    """Run one allowlisted lookup within its timeout and the turn's budget; None when refused, slow or failing."""
    try:
        if not any(tuple(argv[:len(p)]) == p for p in _TRANSFER_ALLOWED_RUNS):
            return None
        remaining = _transfer_deadline(state) - time.monotonic()
        if remaining <= 0:
            return None
        result = subprocess.run(argv, cwd=cwd if cwd and os.path.isdir(cwd) else None, capture_output=True,
                                text=True, timeout=min(_TRANSFER_RUN_TIMEOUT, remaining), stdin=subprocess.DEVNULL)
        return result.stdout if result.returncode == 0 else None
    except Exception:
        return None


def _transfer_read_word(text: str, i: int) -> tuple:
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
        elif ch in ' \t\r\n;&|<>()':
            break
        else:
            word += ch
        i += 1
    return word, i


def _transfer_word_end(text: str, i: int) -> tuple:
    """(the index after the shell word starting at i after blanks, the $( ) and backtick bodies in it, which run);
    the index is -1 when one of them is not closed."""
    while i < len(text) and text[i] in ' \t':
        i += 1
    bodies, quote = [], None
    while i < len(text):
        ch, start = text[i], i
        if quote == "'":
            quote = None if ch == "'" else quote
        elif ch == '\\':
            i += 1
        elif text.startswith('$(', i) or ch == '`':
            i = _transfer_paren_end(text, i + 2) if ch == '$' else _transfer_backtick_end(text, i + 1)
            if i < 0:
                return -1, bodies
            if not text.startswith('$((', start):  # arithmetic runs no commands
                bodies.append(text[start + 2:i] if ch == '$' else text[start + 1:i])
        elif quote == '"':
            quote = None if ch == '"' else quote
        elif ch in '\'"':
            quote = ch
        elif ch in ' \t\r\n;&|<>()':
            break
        i += 1
    return i, bodies


def _transfer_heredoc_bodies(body: str) -> Optional[List[str]]:
    """The $( ) and backtick bodies a heredoc with an unquoted delimiter runs; quotes are plain text there, and a
    backslash escapes the next character. None when one is not closed."""
    bodies, i = [], 0
    while i < len(body):
        start = i
        if body[i] == '\\':
            i += 1
        elif body.startswith('$(', i) or body[i] == '`':
            i = _transfer_paren_end(body, i + 2) if body[i] == '$' else _transfer_backtick_end(body, i + 1)
            if i < 0:
                return None
            if not body.startswith('$((', start):
                bodies.append(body[start + 2:i] if body[start] == '$' else body[start + 1:i])
        i += 1
    return bodies


def _transfer_paren_end(text: str, i: int) -> int:
    """The index of the ) that closes a ( just before i, past quotes and nested parentheses; -1 when none does."""
    depth, quote = 1, None
    while i < len(text):
        ch = text[i]
        if quote == "'":
            quote = None if ch == "'" else quote
        elif ch == '\\':
            i += 1
        elif quote == '"':
            if ch == '"':
                quote = None
            elif text.startswith('$(', i):
                i = _transfer_paren_end(text, i + 2)
                if i < 0:
                    return -1
        elif ch in '\'"':
            quote = ch
        elif ch in '()':
            depth += 1 if ch == '(' else -1
            if depth == 0:
                return i
        i += 1
    return -1


def _transfer_backtick_end(text: str, i: int) -> int:
    """The index of the backtick that closes one just before i; -1 when none does."""
    while i < len(text):
        if text[i] == '`':
            return i
        i += 2 if text[i] == '\\' else 1
    return -1


class _TransferLexer:
    """Reads a command into items in the order they run: ('cmd', argv, operator before, operator after, redirections as
    (fd, 'file' | 'null' | fd copied)), or a '(', '$(', ')', 'func', 'case', 'arm' or 'esac' marker around them."""

    def __init__(self, text: str, depth: int = 0):
        self.text, self.depth, self.i, self.quote = text, depth, 0, None
        self.items, self.argv, self.word, self.in_word = [], [], '', False
        self.before, self.io, self.heredocs, self.case = '', [], [], []
        self.param, self.dollar, self.failed = 0, -1, False

    def read(self) -> Optional[List[tuple]]:
        """The items, or None when the command can't be read the way the shell reads it."""
        if self.depth > _TRANSFER_MAX_DEPTH:
            return None
        while self.i < len(self.text):
            if not (self._quoted() if self.quote else self._unquoted()) or self.failed:
                return None
            self.i += 1
        if self.quote:
            return None
        self._end_command('')
        return None if self.failed else self.items

    def _quoted(self) -> bool:
        ch = self.text[self.i]
        if ch == self.quote:
            self.quote = None
        elif self.quote == '"' and ch in '$`':
            found = self._substitution()
            if found is not None:
                return found
            self.word += ch
        elif self.quote == '"' and ch == '\\' and self.i + 1 < len(self.text):
            self.i += 1
            self.word += self.text[self.i]
        else:
            self.word += ch
        return True

    def _unquoted(self) -> bool:
        text, i, ch = self.text, self.i, self.text[self.i]
        if ch in '$`<>':
            found = self._substitution()
            if found is not None:
                return found
        if ch in '\'"':
            self.quote, self.in_word = ch, True
        elif ch == '\\' and text[i + 1:i + 2] == '\n':
            self.i += 1  # a backslash-newline continues the line
        elif ch == '\\' and i + 1 < len(text):
            self.i += 1
            self.word, self.in_word = self.word + text[self.i], True
        elif ch == '#' and not self.in_word:
            end = text.find('\n', i)
            self.i = (len(text) if end < 0 else end) - 1  # a comment runs to the end of its line
        elif ch == '(':
            self._open()
        elif ch == ')':
            self._close()
        elif ch in '<>' or text.startswith('&>', i):
            self._redirect()
        elif ch in ';&|\n':
            self._separator(ch)
        elif ch in ' \t\r':
            self._end_word()
        else:
            self.word, self.in_word = self.word + self._mark(ch), True
        return True

    def _mark(self, ch: str) -> str:
        """An unquoted character as it goes into the word: a { , or } bash may brace-expand is marked, unless it is part
        of a ${…} parameter."""
        if ch == '{' and (self.param or 0 <= self.dollar == len(self.word) - 1):
            self.param += 1
        elif ch == '}' and self.param:
            self.param -= 1
        elif ch == '$':
            self.dollar = len(self.word)
        elif not self.param:
            return _TRANSFER_BRACE_MARKS.get(ch, ch)
        return ch

    def _substitution(self) -> Optional[bool]:
        """Read a $( ), $(( )), backtick or <( ) substitution starting here; None when none starts here."""
        text, i, inner = self.text, self.i, None
        if text.startswith('$((', i):
            end = _transfer_paren_end(text, i + 2)  # arithmetic runs no commands
        elif text.startswith('$(', i) or (not self.quote and text.startswith(('<(', '>('), i)):
            end = _transfer_paren_end(text, i + 2)
            inner = text[i + 2:end]
        elif text[i] == '`':
            end = _transfer_backtick_end(text, i + 1)
            if end < 0:
                return None  # a lone backtick, as PowerShell writes its escapes
            inner = text[i + 1:end]
        else:
            return None
        if end < 0:
            return False
        if inner is not None:
            items = _TransferLexer(inner, self.depth + 1).read()
            if items is None:
                return False
            self._flush_leading()
            self.items += [('$(', None, self.before, '', None)] + items + [(')', None, '', '', None)]
        self.word, self.in_word, self.i = self.word + _TRANSFER_SUBSTITUTED, True, end
        return True

    def _open(self) -> None:
        if self.case and self.case[-1] == 'pattern':
            return  # the optional ( before a case pattern
        head = self.argv[:-1] if self.argv[-1:] == ['for'] else self.argv
        end = _transfer_paren_end(self.text, self.i + 1) if self.text.startswith('((', self.i) else -1
        if end > 0 and not self.in_word and all(w in _TRANSFER_LEADING_WORDS for w in head):
            # An arithmetic command, or a for loop's (( )) header, is one word that runs nothing.
            self.argv.append(self.text[self.i:end + 1])
            self.i = end
            return
        rest = self.text[self.i + 1:].lstrip(' \t')
        name = self.word if self.in_word else (self.argv[-1] if self.argv else '')
        head = self.argv if self.in_word else self.argv[:-1]
        leading = head[:-1] if head[-1:] == ['function'] else head
        if rest.startswith(')') and all(w in _TRANSFER_LEADING_WORDS for w in leading) and \
                re.match(r'^[\w.:-]+$', name):
            # `name()` or `name ()` defines a function; the body that follows runs only when it's called.
            self.argv, self.word, self.in_word = leading, '', False
            self._flush()  # a then, do or { before it still opens its compound
            self.items.append(('func', [name], self.before, '', None))
            self.i = self.text.index(')', self.i)
            return
        self._end_word()
        self._flush()
        self.items.append(('(', None, self.before, '', None))
        self.before = ''

    def _close(self) -> None:
        if self.case and self.case[-1] == 'pattern':
            # A case pattern ends at its ), and the arm's commands follow.
            self.case[-1] = 'body'
            self.word, self.in_word = '', False
            self.items.append(('arm', None, '', '', None))
            return
        self._end_command('')
        self.items.append((')', None, '', '', None))
        self.before = ';'

    def _redirect(self) -> None:
        """A redirection's operator, an fd number written against it, and its target are not arguments; it is kept as
        the fd it sets and what that fd then holds."""
        text, word = self.text, self.word
        fd = int(word) if self.in_word and word.isdigit() and text[self.i] != '&' else None
        if fd is None:
            self._end_word()
        self.word, self.in_word = '', False
        op = re.match(r'&>>?|<<<|<<-|<<|<>|<&|>>|>&|>\||<|>', text[self.i:]).group(0)
        target, end = _transfer_read_word(text, self.i + len(op))
        if op in ('<<', '<<-'):
            # An unquoted delimiter leaves the body's substitutions to run, with the command, before it does.
            quoted = any(c in text[self.i + len(op):end] for c in '\'"\\')
            self.heredocs.append((target, op == '<<-', None if quoted else self._substitutions_at()))
        else:
            # A target's substitutions run with the command too, such as a here-string's "$(curl …)".
            end, bodies = _transfer_word_end(text, self.i + len(op))
            if end < 0:
                self.failed = True
                return
            self._substitute(bodies, self._substitutions_at() if bodies else None)
        to = fd if fd is not None else 0 if op[0] == '<' else 1
        if op in ('<&', '>&') and (target.isdigit() or target == '-'):
            self.io.append((to, int(target) if target.isdigit() else 'null'))
        else:
            # A heredoc or here-string feeds its text.
            self.io.append((to, 'null' if target == '/dev/null' and op not in ('<<', '<<-', '<<<') else 'file'))
            if op[0] == '&' or (op == '>&' and fd is None):
                self.io.append((2, 1))  # &> and >&file send stderr there too
        self.i = end - 1

    def _separator(self, ch: str) -> None:
        text, i = self.text, self.i
        pair = text[i:i + 2]
        if self.case and self.case[-1] == 'pattern' and ch == '|':
            return  # pattern alternatives
        if self.case and self.case[-1] == 'body' and pair in (';;', ';&'):
            self._end_command(';')
            self.case[-1] = 'pattern'
            self.i += 2 if text.startswith(';;&', i) else 1
            return
        if pair == '|&':
            self.io.append((2, 1))  # |& also pipes stderr, after the command's own redirections
        if pair in ('&&', '||', '|&'):
            self._end_command('|' if pair == '|&' else pair)
            self.i += 1
        else:
            self._end_command(ch if ch in '&|' else ';')
        if ch == '\n':
            self._skip_heredocs()

    def _skip_heredocs(self) -> None:
        """A heredoc's body starts on the next line and is input, not commands, but for the substitutions in one whose
        delimiter is unquoted, which run with its command."""
        text, shift = self.text, 0
        while self.heredocs:
            delimiter, strip_tabs, at = self.heredocs.pop(0)
            lines = []
            while self.i + 1 < len(text):
                end = text.find('\n', self.i + 1)
                line = text[self.i + 1:] if end < 0 else text[self.i + 1:end]
                self.i = len(text) - 1 if end < 0 else end
                if (line.lstrip('\t') if strip_tabs else line) == delimiter:
                    break
                lines.append(line)
            bodies = _transfer_heredoc_bodies('\n'.join(lines)) if at else []
            if bodies is None:
                self.failed = True
                return
            shift += self._substitute(bodies, (at[0] + shift, at[1]) if at else None)

    def _substitutions_at(self) -> tuple:
        """Where substitutions that run with the current command go: after the reserved words that lead it, so they
        run only when it does; (index in items, operator before)."""
        self._flush_leading()
        return len(self.items), self.before

    def _substitute(self, bodies: List[str], at: Optional[tuple]) -> int:
        """Insert each body as a substitution at (index in items, operator before); how many items that added."""
        added = []
        for body in bodies:
            items = _TransferLexer(body, self.depth + 1).read()
            if items is None:
                self.failed = True
                return 0
            added += [('$(', None, at[1], '', None)] + items + [(')', None, '', '', None)]
        if added:
            self.items[at[0]:at[0]] = added
        return len(added)

    def _end_word(self) -> None:
        if not self.in_word:
            return
        marked, state = self.word, self.case[-1] if self.case else None
        self.word, self.in_word, self.param, self.dollar = '', False, 0, -1
        word = marked.translate(_TRANSFER_UNMARK)
        at_start = all(w in _TRANSFER_LEADING_WORDS for w in self.argv)
        if state == 'head':
            self.case[-1] = 'pattern' if word == 'in' else 'head'  # `case WORD in` runs nothing
        elif state == 'pattern':
            if word == 'esac':
                self._esac()
        elif word == 'case' and at_start:
            self._flush()
            self.items.append(('case', None, self.before, '', None))
            self.case.append('head')
        elif word == 'esac' and at_start and state == 'body':
            self._esac()
        elif word == 'do' and self._loop_header():
            self._end_command(';')  # `for x do` reads as `for x; do`
            self.argv.append(word)
        elif _transfer_is_assignment(word) and all(_transfer_is_assignment(w) or w in _TRANSFER_LEADING_WORDS
                                                   for w in self.argv):
            self.argv.append(word)  # an assignment before the command is not brace-expanded
        else:
            words = [w.translate(_TRANSFER_UNMARK) for w in _transfer_braces(marked)]
            self.argv += words if len(words) == 1 else [w for w in words if w]  # an empty result is no word

    def _loop_header(self) -> bool:
        """Whether the words so far end in a for or select header with no `in` list, which a `do` may follow at once."""
        n = 0
        while n < len(self.argv) and self.argv[n] in _TRANSFER_LEADING_WORDS:
            n += 1
        core = self.argv[n:]
        return len(core) == 2 and core[0] in ('for', 'select') and core[1] != 'in'

    def _esac(self) -> None:
        self._flush()
        self.case.pop()
        self.items.append(('esac', None, '', '', None))

    def _flush_leading(self) -> None:
        """Emit the reserved words that lead the current command, so a substitution in it runs only when they let it."""
        n = 0
        while n < len(self.argv) and (self.argv[n] in _TRANSFER_LEADING_WORDS or self.argv[n] == 'function'):
            n += 2 if self.argv[n] == 'function' else 1
        if n:
            self.items.append(('cmd', self.argv[:n], self.before, '', []))
            self.argv, self.before = self.argv[n:], ''

    def _flush(self) -> None:
        """Emit the reserved words read so far ahead of the compound command they lead."""
        if self.argv:
            self.items.append(('cmd', self.argv, self.before, '', self.io))
            self.argv, self.io, self.before = [], [], ''

    def _end_command(self, after: str) -> None:
        self._end_word()
        if self.argv:
            self.items.append(('cmd', self.argv, self.before, after, self.io))
            self.before = after
        else:
            if self.items and self.items[-1][0] in (')', 'esac') and (after or self.io):
                # A pipe, redirection or & after a subshell or case applies to the whole compound.
                kind, _, before, _, _ = self.items[-1]
                self.items[-1] = (kind, None, before, after, self.io)
            if after not in (';', ''):
                self.before = after
        self.argv, self.io = [], []


def _transfer_braces(word: str) -> List[str]:
    """A word's brace expansions, as bash makes them from its marked { , }: a {a,b} list or a {1..3} or {a..c}
    sequence, nested and in order; a brace that is neither is kept as written."""
    start = word.find('\1')
    while start >= 0:
        depth, commas, end = 0, [], -1
        for j in range(start, len(word)):
            depth += {'\1': 1, '\3': -1}.get(word[j], 0)
            if word[j] == '\2' and depth == 1:
                commas.append(j)
            if depth == 0:
                end = j
                break
        bounds = [start] + commas + [end]
        parts = [word[a + 1:b] for a, b in zip(bounds, bounds[1:])] if commas and end > 0 else \
            _transfer_sequence(word[start + 1:end]) if end > 0 else None
        if parts is not None:
            import itertools
            tails = _transfer_braces(word[end + 1:])
            return list(itertools.islice((word[:start] + part + tail for whole in parts
                                          for part in _transfer_braces(whole) for tail in tails), _TRANSFER_MAX_WORDS))
        start = word.find('\1', start + 1)
    return [word]


def _transfer_sequence(body: str) -> Optional[List[str]]:
    """The words a {x..y} or {x..y..step} sequence of integers or letters makes, as bash 5 makes them; None for any
    other brace."""
    match = re.match(r'^(-?\d+|[A-Za-z])\.\.(-?\d+|[A-Za-z])(?:\.\.(-?\d+))?$', body)
    if not match or match.group(1).isalpha() != match.group(2).isalpha():
        return None
    first, last = match.group(1), match.group(2)
    step = abs(int(match.group(3) or 1)) or 1
    a, b = (ord(first), ord(last)) if first.isalpha() else (int(first), int(last))
    values = range(a, b + (1 if b >= a else -1), step if b >= a else -step)[:_TRANSFER_MAX_WORDS]
    if first.isalpha():
        return [chr(c) for c in values]
    width = max(len(first), len(last)) if re.match(r'^-?0\d', first) or re.match(r'^-?0\d', last) else 0
    return [str(n).zfill(width) for n in values]


def _transfer_words(text: str) -> List[str]:
    """A string's words, split as the shell splits them, without its operators."""
    return [w for item in (_TransferLexer(text).read() or []) if item[0] == 'cmd' for w in item[1]]


def _transfer_env(env: Dict, key: str) -> str:
    """A variable as the command sees it: set or unset in the command, else inherited from the hook's environment."""
    if key in env:
        return env[key]
    return '' if _TRANSFER_CLEAN_ENV in env else os.environ.get(key, '')


def _transfer_is_assignment(token: str) -> bool:
    """NAME=value, or NAME+=value, which appends to the value."""
    name = token.split('=', 1)[0]
    name = name[:-1] if name.endswith('+') else name
    return '=' in token and not token.startswith('-') and name.replace('_', '').isalnum()


def _transfer_appends(words: List[str], shell: Dict) -> List[str]:
    """The words with each leading NAME+=value made NAME=value: appended to the earlier or shell value in a command of
    only assignments, and unknown before a command, where bash 3 appends to nothing and bash 5 and zsh to that value."""
    given, words, bare = {}, list(words), all(_transfer_is_assignment(w) for w in words)
    for i, word in enumerate(words):
        if not _transfer_is_assignment(word):
            break
        name, value = word.split('=', 1)
        if name.endswith('+'):
            name = name[:-1]
            before = given[name] if name in given else _transfer_variable(shell, name)[0] or ''
            value = (before if bare else _TRANSFER_SUBSTITUTED) + value
            words[i] = name + '=' + value
        given[name] = value
    return words


def _transfer_program_name(token: str) -> str:
    """A program as it's matched: its file name in lower case, without a Windows .exe."""
    name = re.split(r'[\\/]', token)[-1].lower()
    return name[:-4] if name.endswith('.exe') else name


def _transfer_program(argv: List[str], cwd: Optional[str]) -> tuple:
    """(program, its args, inline VAR=value settings, folder it runs in, whether it runs as another user)."""
    env, argv, i, wrapper, operands, options_done, other_user = {}, list(argv), 0, None, 0, False, False
    while i < len(argv):
        token = argv[i]
        i += 1
        if _transfer_is_assignment(token) and (wrapper is None or options_done or wrapper in ('env', 'sudo')):
            key, value = token.split('=', 1)
            env[key] = value
            continue
        if wrapper is not None and not options_done and token == '--':
            options_done = True
            continue
        if wrapper == 'env' and not options_done and token in ('-', '--ignore-environment'):
            env.clear()
            env[_TRANSFER_CLEAN_ENV] = ''
            continue
        if wrapper is not None and not options_done and token.startswith('-') and token != '-':
            if wrapper == 'command' and token in ('-v', '-V'):
                return None, [], env, cwd, other_user  # only looks the command up
            i, cwd = _transfer_wrapper_option(wrapper, token, argv, i, env, cwd)
            continue
        if wrapper is not None and operands > 0:
            operands -= 1
            continue
        if _transfer_program_name(token) in _TRANSFER_WRAPPERS:
            wrapper, options_done = _transfer_program_name(token), False
            operands = _TRANSFER_WRAPPERS[wrapper][2]
            other_user = other_user or wrapper in ('sudo', 'doas')
            continue
        return _transfer_program_name(token), argv[i:], env, cwd, other_user
    return None, [], env, cwd, other_user


def _transfer_cd_words(args: List[str]) -> tuple:
    """A cd's option letters and its folders: options come first, -- ends them, and a lone - is a folder."""
    folders, letters = list(args), ''
    while folders and folders[0].startswith('-') and folders[0] != '-':
        option = folders.pop(0)
        if option == '--':
            break
        letters += option[1:]
    return letters, folders


def _transfer_cd_rejected(args: List[str]) -> bool:
    """Whether a cd has an option that bash 3, bash 5 and zsh all reject, so it fails and stays."""
    return any(c not in 'LPeqs@0123456789' for c in _transfer_cd_words(args)[0])


def _transfer_cd(args: List[str], cwd: Optional[str]) -> Optional[str]:
    """The folder a cd with args lands in from cwd: cwd when no shell takes one of its options, and None when bash 3,
    bash 5 and zsh land in different places or the folder is not known."""
    letters, folders = _transfer_cd_words(args)
    if _transfer_cd_rejected(args):
        return cwd
    if len(folders) > 1 or any(c not in 'LP' for c in letters):
        return None  # bash 5 rejects a second folder, bash 3 ignores it and zsh substitutes it; -e, -q, -s, -@ vary
    # Bash follows the last of -L and -P, zsh any -P; -P resolves each link, so a later `cd ..` leaves the real folder.
    last = letters[max(letters.rfind('L'), letters.rfind('P'))] if letters else 'L'
    path = folders[0] if folders else '~'
    landed = {_transfer_move(cwd, path, physical) for physical in {last == 'P', 'P' in letters}}
    return landed.pop() if len(landed) == 1 else None


def _transfer_move(cwd: Optional[str], path: str, physical: bool) -> Optional[str]:
    """The folder a cd to path lands in from cwd, links resolved when physical; None when cwd is not known, for `cd -`,
    or when the folder is not there now, so the cd may have failed."""
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        if cwd is None or path == '-':
            return None
        path = os.path.join(cwd, path)
    landed = os.path.realpath(path) if physical else os.path.normpath(path)
    return landed if os.path.isdir(path) and os.path.isdir(landed) else None


def _transfer_wrapper_option(wrapper: str, token: str, argv: List[str], i: int, env: Dict, cwd: Optional[str]) -> tuple:
    """Read one wrapper option, and its value when it takes one; (the next index, the folder the command runs in)."""
    short_flags, long_flags, _ = _TRANSFER_WRAPPERS[wrapper]
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
                env[_TRANSFER_CLEAN_ENV] = ''
            if flag in short_flags:
                name, value = '-' + flag, token[j:]
                if not value and i < len(argv):
                    value, i = argv[i], i + 1
                break
    if value is not None and name in {'env': ('-C', '--chdir'), 'sudo': ('-D', '--chdir')}.get(wrapper, ()):
        cwd = _transfer_move(cwd, value, True)  # a chdir, so links resolve
    elif value is not None and name in ('-S', '--split-string') and wrapper == 'env':
        argv[i:i] = _transfer_words(value)
    elif value is not None and name in ('-u', '--unset') and wrapper == 'env':
        env[value] = ''
    return i, cwd


def _transfer_both(a: Optional[bool], b: Optional[bool]) -> Optional[bool]:
    """Whether two conditions both hold: False when either fails, None when either is unknown."""
    if a is False or b is False:
        return False
    return None if a is None or b is None else True


def _transfer_follows(before: str, status: Optional[int]) -> bool:
    """Whether a pipeline runs after its && or ||, given the last literal true (0) or false (1)."""
    return not ((before == '&&' and status == 1) or (before == '||' and status == 0))


def _transfer_runs(shell: Dict) -> Optional[bool]:
    return shell['frames'][-1]['runs'] if shell['frames'] else shell['live']


def _transfer_snapshot(shell: Dict) -> tuple:
    return shell['cwd'], dict(shell['vars']), dict(shell['functions'])


def _transfer_restore(shell: Dict, saved: Optional[tuple], subshell: bool = False) -> None:
    """Undo the cd and variables since saved. A subshell also undoes its function definitions; one defined in a body
    that may not have run is kept, so its calls are still followed."""
    if saved:
        shell['cwd'], shell['vars'] = saved[0], dict(saved[1])
        if subshell:
            shell['functions'] = dict(saved[2])


def _transfer_changes(shell: Dict, saved: tuple) -> set:
    """What a cd, assignment, export or unset changed since saved: '' for the folder, else each variable."""
    now = shell['vars']
    names = {key for key in set(saved[1]) | set(now) if saved[1].get(key) != now.get(key)}
    return names | ({''} if shell['cwd'] != saved[0] else set())


def _transfer_forget(shell: Dict, names: set) -> None:
    """Make what may or may not have changed unknown: the folder, so no config is read from a guessed one, and each
    variable, which then reads like a substitution's output."""
    if '' in names:
        shell['cwd'] = None
    shell['vars'].update((name, (_TRANSFER_SUBSTITUTED, None)) for name in names if name)


def _transfer_variable(shell: Dict, name: str) -> tuple:
    """A shell variable as (its value or None when unset, whether it is exported); one the shell never set is the hook's
    own environment's, which is exported."""
    if name in shell['vars']:
        return shell['vars'][name]
    inherited = _TRANSFER_CLEAN_ENV not in shell['vars'] and name in os.environ
    return (os.environ[name] if inherited else None), inherited


def _transfer_exported(shell: Dict) -> Dict:
    """The variables a command gets from the shell; one unset or not exported hides the hook's own value, and one that
    may or may not be exported (None) reads like a substitution's output."""
    return {name: _TRANSFER_SUBSTITUTED if exported is None else value if exported and value is not None else ''
            for name, (value, exported) in shell['vars'].items()}


def _transfer_doubt(pipe: Dict) -> bool:
    """Whether a pipeline runs only through an && or || after a status that is not known."""
    return pipe['prior'] is None and pipe['before'] in ('&&', '||')


def _transfer_pop(frames: List[Dict], kinds: tuple) -> Optional[Dict]:
    """Close the innermost compound command of one of these kinds."""
    while frames:
        frame = frames.pop()
        if frame['kind'] in kinds:
            return frame
    return None


def _transfer_pipeline(before: str, runs: Optional[bool], shell: Dict) -> None:
    """Start a pipeline: whether it runs, and the operator and status before it, which settle the status it leaves."""
    shell['pipe'] = {'before': before, 'prior': shell['status'], 'runs': runs, 'negate': False, 'leading': False}


def _transfer_begin(before: str, shell: Dict) -> None:
    """Start the pipeline a command or compound begins, unless it continues one after a | or a leading `!`; && and ||
    decide whether the whole pipeline runs, and any other operator also starts a list."""
    if before == '|':
        # A later command of a pipeline sees the status from before it in bash 5 but 0 in bash 3, so only 0 is known.
        shell['status'] = 0 if shell['pipe']['prior'] == 0 else None
    if before == '|' or shell['pipe']['leading']:
        return
    if before not in ('&&', '||'):
        shell['start'] = _transfer_snapshot(shell)
    _transfer_pipeline(before, _transfer_both(_transfer_runs(shell), _transfer_follows(before, shell['status'])), shell)


def _transfer_end(shell: Dict) -> None:
    """Set the status a finished pipeline leaves: the one before it when it never ran, else its last command's, flipped
    by `!`, and unknown when an unknown status let it run and skipping it would have left another."""
    pipe, status = shell['pipe'], shell['status']
    if pipe['runs'] is False:
        status = pipe['prior']
    else:
        status = {0: 1, 1: 0}.get(status) if pipe['negate'] else status
        skipped = {'&&': 1, '||': 0}.get(pipe['before'])
        if pipe['prior'] is None and skipped is not None and status != skipped:
            status = None
    shell['status'] = status


def _transfer_finish(after: str, shell: Dict) -> None:
    """End a pipeline element: the pipeline ends unless a | follows, and a backgrounded list runs in a subshell."""
    if after != '|':
        _transfer_end(shell)
    if after == '&':
        _transfer_restore(shell, shell['start'], True)
        shell['status'] = 0  # starting a background job succeeds


def _transfer_open(kind: str, runs: Optional[bool], shell: Dict, head: tuple) -> None:
    """Open a compound command, setting the pipeline and list around it aside until it closes; a function's body is
    only defined, so it never runs here. head is the item that starts it, kept for a function's calls."""
    name = shell['function'] if kind != '$(' else None
    if name is not None:
        runs, shell['function'] = False, None
    if kind != '$(':
        shell['pipe']['leading'] = False  # a substitution runs inside the command that a leading `!` still awaits
    frames = shell['frames']
    frames.append({'kind': kind, 'base': runs, 'runs': runs, 'rel': True, 'alive': True, 'decided': False,
                   'saved': None, 'state': _transfer_snapshot(shell), 'status': shell['status'],
                   'calls': len(shell['calls']), 'piped_in': shell.pop('opening_pipe', False),
                   'piped_out': _transfer_ends_piped(head, shell),
                   'outer': (shell['pipe'], shell['start']), 'defines': name, 'head': (shell['at'], head),
                   'iterates': None, 'repeats': False, 'forever': False, 'counter': None,
                   'left': None, 'doubt': _transfer_doubt(shell['pipe']),
                   'jumped': None,
                   'unsure': set(), 'outside': _transfer_liveness(shell)})
    shell['start'] = _transfer_snapshot(shell)  # the body starts a list of its own
    _transfer_pipeline('', runs, shell)


def _transfer_status(frame: Dict, status: Optional[int]) -> Optional[int]:
    """The status a closed compound leaves: a jump's that ended it, 0 for a definition or an if or loop that ran no
    body, the one before it when it never ran, unknown when a body may have run, else its last command's."""
    kind, rel = frame['kind'], frame['rel']
    if frame['left']:
        return frame['left'][0]
    if frame['defines'] is not None:
        return 0
    if frame['base'] is False or kind == '$(':
        return frame['status']
    if kind == 'if' and rel is not True and frame['decided'] is not True:
        return 0 if rel is False and frame['decided'] is False else None
    if kind in _TRANSFER_LOOPS:
        if rel is False:
            return 0
        return status if kind == 'for' and rel else None
    if kind == 'case':
        return 0 if rel is False else None
    return status


def _transfer_settle(frame: Dict, tail: tuple, shell: Dict) -> None:
    """Close a compound: set the status it leaves, resume the pipeline and list around it, and keep a defined
    function's body, from its head item to tail, for the calls that run it."""
    shell['status'] = _transfer_status(frame, shell['status'])
    shell['pipe'], shell['start'] = frame['outer']
    if frame['defines'] is not None and shell['pipe']['runs'] is not False:
        at, head = frame['head']
        maybe = shell['pipe']['runs'] is None or _transfer_doubt(shell['pipe'])  # the definition may not have run
        shell['functions'][frame['defines']] = [head] + shell['items'][at + 1:shell['at']] + [tail], maybe


def _transfer_leave(frame: Dict, shell: Dict) -> None:
    """Leave a body that may not have run: what it changed is remembered as unknown, and the state before it returns."""
    if frame['saved']:
        frame['unsure'] |= _transfer_changes(shell, frame['saved'])
        _transfer_restore(shell, frame['saved'])
        frame['saved'] = None


def _transfer_doubtful(frame: Dict, shell: Dict) -> None:
    """At a compound's end, make unknown what may not have happened: a body that may not have run, what followed a jump
    that may have run, and all of it when an unknown status guarded the compound or it is a loop that may repeat."""
    for since in (frame['jumped'], frame['state'] if frame['doubt'] or frame['repeats'] else None):
        if since:
            frame['unsure'] |= _transfer_changes(shell, since)
    _transfer_leave(frame, shell)
    _transfer_forget(shell, frame['unsure'])


def _transfer_wrap(shell: Dict, first: int, layer: tuple) -> None:
    """Wrap each call from first on in one more level of (piped in, piped out, redirections)."""
    calls = shell['calls']
    for i in range(first, len(calls)):
        calls[i] = calls[i][:5] + (calls[i][5] + (layer,),)


_TRANSFER_JUMP_MARKS = ('runs', 'alive', 'left', 'jumped', 'repeats')


def _transfer_liveness(shell: Dict) -> tuple:
    """What a return, exit, break or continue may change in the open compounds and the shell, to put back."""
    return [tuple(f[key] for key in _TRANSFER_JUMP_MARKS) for f in shell['frames']], shell['live']


def _transfer_revive(outside: tuple, shell: Dict) -> None:
    """Undo what a return, exit, break or continue did beyond a compound that turned out to run in a subshell."""
    for frame, marks in zip(shell['frames'], outside[0]):
        frame.update(zip(_TRANSFER_JUMP_MARKS, marks))
    shell['live'] = outside[1]


def _transfer_soften(outside: tuple, shell: Dict) -> None:
    """Make what a return, exit, break or continue did beyond a call that may not run a maybe: what follows may run,
    and the compound it would have ended leaves an unknown status."""
    for frame, marks in zip(shell['frames'], outside[0]):
        if frame['alive'] is False and marks[1] is not False:
            frame['alive'], frame['runs'] = None, _transfer_both(marks[0], None)
            frame['jumped'] = frame['jumped'] or _transfer_snapshot(shell)
        if frame['left'] != marks[2]:
            frame['left'] = (None,)
        frame['repeats'] = marks[4]
    if shell['live'] is False and outside[1] is not False:
        shell['live'] = None


def _transfer_close(frame: Dict, layer: tuple, shell: Dict, background: bool = False) -> None:
    """Wrap a closed compound's commands in the pipes and redirections around it. Piped or backgrounded, its changes end
    with it; as a pipeline's last command, zsh keeps them and bash does not, so they are unknown."""
    _transfer_wrap(shell, frame['calls'], layer)
    if layer[0] and not (layer[1] or background or frame['kind'] in ('(', '$(')):
        _transfer_unsure(shell, frame['state'])
        _transfer_revive(frame['outside'], shell)
    elif layer[0] or layer[1] or background:
        _transfer_restore(shell, frame['state'], True)
        _transfer_revive(frame['outside'], shell)


def _transfer_unsure(shell: Dict, saved: tuple) -> None:
    """Make what changed since saved unknown, for what may or may not have happened: the folder and variables, and
    each function defined or removed, which then may or may not be there."""
    _transfer_forget(shell, _transfer_changes(shell, saved))
    for name, known in saved[2].items():
        shell['functions'].setdefault(name, (known[0], True))
    for name, known in list(shell['functions'].items()):
        if saved[2].get(name) is not known:
            shell['functions'][name] = known[0], True


def _transfer_branch(keyword: str, frame: Dict, shell: Dict) -> None:
    """Start the body a then, elif, else or do opens: run, skipped or maybe run, by the literal status before it, as
    far as the compound itself runs."""
    holds = {0: True, 1: False}.get(shell['status'])
    if keyword == 'then' and frame['kind'] == 'if':
        taken = _transfer_both(frame['rel'], holds)
        frame['decided'] = True if taken else None if taken is None and frame['decided'] is False else frame['decided']
    elif keyword in ('elif', 'else') and frame['kind'] == 'if':
        _transfer_leave(frame, shell)
        taken = {True: False, False: True}.get(frame['decided'])
    elif keyword == 'do' and frame['kind'] in _TRANSFER_LOOPS:
        if frame['kind'] in ('for', 'select'):
            holds = frame['iterates']
        elif frame['kind'] == 'until' and holds is not None:
            holds = not holds
        if frame['kind'] in ('while', 'until'):
            frame['forever'] = holds is True  # a test that is a literal holds every time
        taken = _transfer_both(frame['rel'], holds)
    else:
        return
    _transfer_take(frame, taken, shell)


def _transfer_take(frame: Dict, taken: Optional[bool], shell: Dict) -> None:
    """Set whether the body now starting runs; a body that may not run keeps the state before it to return to."""
    frame['rel'], frame['runs'] = taken, _transfer_both(_transfer_both(frame['base'], taken), frame['alive'])
    if taken is None and frame['saved'] is None:
        frame['saved'] = _transfer_snapshot(shell)
    shell['start'] = _transfer_snapshot(shell)


def _transfer_jump(program: str, args: List[str], shell: Dict) -> None:
    """Stop what a return, exit, break or continue cuts short: the rest of the function, the shell or subshell, or the
    loop body. One that may not run leaves the rest maybe running, and what that rest changes unknown."""
    frames, floor = shell['frames'], shell['floor']
    # None of them reaches past a subshell, and a break or continue does not leave the function it is in.
    bound = max([i for i, f in enumerate(frames) if f['kind'] in ('(', '$(') or f['piped_in'] or f['piped_out']],
                default=-1)
    if program in ('break', 'continue'):
        # A loop that is itself piped is its own subshell, so a break inside still reaches it.
        loops = [i for i, f in enumerate(frames) if f['kind'] in _TRANSFER_LOOPS and i >= max(bound, floor or 0)]
        if not loops:
            return
        target = loops[-min(int(args[0]) if args and args[0].isdigit() and int(args[0]) else 1, len(loops))]
    elif program == 'return':
        if floor is None:
            return  # outside a function, return only complains
        target = max(floor, bound)
    else:
        target = max(bound, 0)
    # Sure once the compound it ends is running: nothing between here and there can skip it, and an if or case it ends
    # surely took the branch it is in. It ends the whole shell only when the outermost compound surely runs as well.
    sure = not _transfer_doubt(shell['pipe']) and \
        all(f['rel'] is True and f['alive'] is True and not f['doubt'] for f in frames[target + 1:]) and \
        not any(f['kind'] in ('if', 'case') and f['rel'] is not True for f in frames[target:target + 1])
    whole = sure and shell['pipe']['runs'] is True if not frames else \
        sure and frames[0]['base'] is True and frames[0]['rel'] is True and not frames[0]['doubt']
    for frame in frames[target:]:
        frame['alive'] = _transfer_both(frame['alive'], False if sure else None)
        frame['runs'] = _transfer_both(frame['runs'], frame['alive'])
        # A sure break, return or exit ends the loops it leaves for good, while a continue runs its own loop again.
        frame['repeats'] = frame['repeats'] and not (sure and (program != 'continue' or frame is not frames[target]))
    if program == 'exit' and bound < 0:
        shell['live'] = _transfer_both(shell['live'], False if whole else None)
    if target < len(frames):
        # The compound ends with the jump's status, unknown when it may not have jumped, or may have earlier. A `!`
        # still flips a break or continue, while a return or exit leaves at once.
        status = int(shell['pipe']['negate']) if program in ('break', 'continue') else shell['status']
        frames[target]['left'] = (status if sure and not frames[target]['left'] else None,)
        if not sure and frames[target]['jumped'] is None:
            frames[target]['jumped'] = _transfer_snapshot(shell)


def _transfer_keyword(words: List[str], item: tuple, shell: Dict) -> List[str]:
    """Apply a command's leading reserved word to the open compound commands and its pipeline; the words after it."""
    keyword, rest, frames = words[0], words[1:], shell['frames']
    if keyword in ('{', 'if') + _TRANSFER_LOOPS:
        _transfer_open(keyword, shell['pipe']['runs'], shell, ('cmd', words, '', item[3], item[4]))
        if keyword in _TRANSFER_LOOPS:
            loop = frames[-1]
            loop['iterates'], loop['repeats'], loop['forever'], loop['counter'] = _transfer_loop(keyword, rest, shell)
            end = shell['ends'].get((shell['at'], len(item[1]) - len(words)), ('', None))[1]
            _transfer_touched(loop, _transfer_loop_commands(loop, shell, end))
        return [] if keyword in ('for', 'select') else rest  # a for loop's header runs nothing
    if keyword in ('!', 'function'):
        # `!` flips the status of the whole pipeline it leads, a compound included, and a second one flips it back.
        if keyword == 'function':
            shell['function'], rest = (rest[0] if rest else ''), rest[1:]
        else:
            shell['pipe']['negate'] = not shell['pipe']['negate']
        shell['pipe']['leading'] = True
        return rest
    if keyword in ('}', 'fi', 'done'):
        frame = _transfer_pop(frames, {'}': ('{',), 'fi': ('if',), 'done': _TRANSFER_LOOPS}[keyword])
        if frame:
            endless = _transfer_endless(frame, shell)
            _transfer_doubtful(frame, shell)
            _transfer_settle(frame, ('cmd', [keyword], item[2], '', item[4]), shell)
            if endless:
                _transfer_jump('exit', [], shell)  # nothing after a loop that never ends runs
        shell['closed'] = frame
        return rest
    if frames:
        _transfer_branch(keyword, frames[-1], shell)
    _transfer_pipeline('', _transfer_runs(shell), shell)
    return rest


def _transfer_loop(keyword: str, rest: List[str], shell: Dict) -> tuple:
    """(whether a loop runs its body at least once: True, False or None; whether it may run it again; whether its test
    never fails; an arithmetic for's counter), from its header, or the positional parameters when it has no `in`."""
    if keyword == 'for' and rest[:1] and rest[0].startswith('(('):
        return _transfer_counted(rest[0])
    if keyword not in ('for', 'select'):
        return None, True, False, None
    listed = rest[2:] if rest[1:2] == ['in'] else shell['args']
    literal = keyword == 'for' and any('$' not in w and _TRANSFER_SUBSTITUTED not in w for w in listed or [])
    plain = listed is not None and len(listed) == 1 and not any(c in listed[0] for c in '$*?[\0')
    return True if literal else False if listed == [] else None, keyword != 'for' or not plain, False, None


_TRANSFER_NAME = r'([A-Za-z_][A-Za-z0-9_]*)'
_TRANSFER_NUMBER = r'(0|[1-9][0-9]*)'
_TRANSFER_TESTS = {'<': int.__lt__, '<=': int.__le__, '>': int.__gt__, '>=': int.__ge__, '==': int.__eq__,
                   '!=': int.__ne__}


def _transfer_counted(word: str) -> tuple:
    """_transfer_loop's answer for an arithmetic for: read when it starts its counter at a number, tests it against a
    number and steps it by a number; with no test it runs until something leaves it."""
    parts = [p.strip() for p in word[2:-2].split(';')] if word.endswith('))') else []
    if len(parts) != 3:
        return None, True, False, None
    start = re.fullmatch(_TRANSFER_NAME + r'\s*=\s*' + _TRANSFER_NUMBER, parts[0])
    if parts[0] and not start:
        return None, True, False, None
    if not parts[1]:
        return True, True, True, None
    test = re.fullmatch(_TRANSFER_NAME + r'\s*(<=|>=|==|!=|<|>)\s*' + _TRANSFER_NUMBER, parts[1])
    by = _transfer_step(parts[2], start.group(1)) if start else None
    if not (test and by is not None and start.group(1) == test.group(1)):
        return None, True, False, None
    a, holds, b = int(start.group(2)), _TRANSFER_TESTS[test.group(2)], int(test.group(3))
    if not holds(a, b):
        return False, False, False, start.group(1)
    # The counter runs a, a + by, a + 2 by, ...: a != test fails only if it lands on the number, any other only if
    # the counter moves toward failing it.
    if test.group(2) == '!=':
        forever = by == 0 or (b - a) % by != 0 or (b - a) // by < 0
    elif test.group(2) == '==':
        forever = by == 0
    else:
        forever = by == 0 or (by > 0) == (test.group(2) in ('>', '>='))
    return True, holds(a + by, b), forever, start.group(1)


def _transfer_step(step: str, name: str) -> Optional[int]:
    """How much an arithmetic for's step adds to its counter: nothing, ++ or --, += or -= a number, or the counter
    plus or minus a number; None for anything else."""
    if not step:
        return 0
    match = re.fullmatch(r'(\+\+|--)?\s*' + _TRANSFER_NAME + r'\s*(\+\+|--)?', step)
    if match and match.group(2) == name and bool(match.group(1)) != bool(match.group(3)):
        return 1 if (match.group(1) or match.group(3)) == '++' else -1
    match = re.fullmatch(_TRANSFER_NAME + r'\s*([+-])=\s*' + _TRANSFER_NUMBER, step) or \
        re.fullmatch(_TRANSFER_NAME + r'\s*=\s*' + re.escape(name) + r'\s*([+-])\s*' + _TRANSFER_NUMBER, step)
    if match and match.group(1) == name:
        return int(match.group(3)) * (1 if match.group(2) == '+' else -1)
    return None


def _transfer_loop_commands(frame: Dict, shell: Dict, end: Optional[int]) -> Optional[List[List[str]]]:
    """The words of each command a loop runs in this shell, from its condition up to the item end, leaving out those in
    a subshell or substitution; None when its end is not known."""
    at, head = frame['head']
    if end is None:
        return None
    return [head[1][2 if head[1][0] in ('for', 'select') else 1:]] + _transfer_own_commands(shell['items'][at + 1:end])


def _transfer_own_commands(items: List[tuple]) -> List[List[str]]:
    """The words of each command in items that runs in this shell, not in a subshell or substitution."""
    depth, commands = 0, []
    for item in items:
        depth += 1 if item[0] in ('(', '$(') else -1 if item[0] == ')' else 0
        if item[0] == 'cmd' and not depth:
            commands.append(item[1])
    return commands


def _transfer_touched(frame: Dict, commands: Optional[List[List[str]]]) -> None:
    """An arithmetic for that may change its counter may run its body again, and its test may fail after all; a
    plain $name or ${name} only reads it."""
    name = frame['counter']
    if not name:
        return
    for word in (w for words in commands or [[name]] for w in words):
        for match in re.finditer(r'(?<![A-Za-z0-9_])' + name + r'(?![A-Za-z0-9_])', word):
            before, after = word[:match.start()], word[match.end():]
            if not (before.endswith('$') or (before.endswith('${') and after.startswith('}'))):
                frame['repeats'], frame['forever'] = True, False
                return


def _transfer_leaves(commands: List[List[str]], shell: Dict, called: bool, seen: set) -> bool:
    """Whether commands could leave the loop they run in: a break, a continue past it, a return (but in a called
    function, where it only ends the call), eval or source, or a call to a function that could."""
    for words in commands:
        for k, word in enumerate(words):
            count = words[k + 1] if k + 1 < len(words) else '1'
            if word in ('break', 'eval', 'source', '.') or (word == 'return' and not called) or \
                    (word == 'continue' and not (re.fullmatch(r'[0-9]+', count) and int(count) <= 1)):
                return True
            if word in shell['functions'] and word not in seen:
                seen.add(word)
                if _transfer_leaves(_transfer_own_commands(shell['functions'][word][0]), shell, True, seen):
                    return True
    return False


def _transfer_endless(frame: Dict, shell: Dict) -> bool:
    """Whether a loop now closing never ends: its test never fails, and nothing it runs in this shell could leave
    it."""
    commands = _transfer_loop_commands(frame, shell, shell['at']) if frame['kind'] in _TRANSFER_LOOPS else None
    return bool(commands is not None and frame['forever'] and frame['base'] is not False and
                not _transfer_leaves(commands, shell, False, set()))


def _transfer_arithmetic(word: str) -> Optional[int]:
    """The status a (( N )) with a decimal number leaves: 0 for one that is not zero, 1 for zero; None otherwise. A
    leading zero is octal in bash and decimal in zsh, but zero either way."""
    inner = word[2:-2].strip() if word.startswith('((') and word.endswith('))') else ''
    return int(int(inner) == 0) if re.fullmatch(r'0+|[1-9][0-9]*', inner) else None


def _transfer_marker(item: tuple, shell: Dict) -> None:
    """Apply a subshell, substitution, function or case marker to the open compound commands."""
    kind, words, before, after, io = item
    frames, pipe = shell['frames'], shell['pipe']
    if kind in ('(', 'case'):
        _transfer_begin(before, shell)
        shell['opening_pipe'] = before == '|'
        _transfer_open(kind, shell['pipe']['runs'], shell, (kind, None, '', '', None))
        if kind == 'case':
            frames[-1]['rel'] = frames[-1]['runs'] = False  # nothing runs until an arm opens
    elif kind == '$(':
        # A substitution runs when its command does, and reads the stdin piped into that command.
        runs = pipe['runs'] if before == '|' or pipe['leading'] else \
            _transfer_both(_transfer_runs(shell), _transfer_follows(before, shell['status']))
        shell['opening_pipe'] = before == '|'
        _transfer_open(kind, runs, shell, item)
    elif kind == 'func':
        _transfer_begin(before, shell)
        shell['function'], shell['pipe']['leading'] = words[0], True
    elif kind == 'arm' and frames and frames[-1]['kind'] == 'case':
        _transfer_leave(frames[-1], shell)
        _transfer_take(frames[-1], None, shell)
    elif kind in (')', 'esac'):
        frame = _transfer_pop(frames, ('(', '$(') if kind == ')' else ('case',))
        if frame:
            # A subshell's cd, exports and functions end with it.
            if kind == ')':
                _transfer_restore(shell, frame['state'], True)
            else:
                _transfer_doubtful(frame, shell)
            _transfer_settle(frame, (kind, None, before, '', io), shell)
            if frame['kind'] == '$(':
                _transfer_close(frame, (frame['piped_in'], False, ((1, 'null'),)), shell)  # its output is captured
            else:
                _transfer_close(frame, (frame['piped_in'], after == '|', tuple(io or ())), shell, after == '&')
                _transfer_finish(after, shell)


def _transfer_builtin(program: str, args: List[str], cwd: Optional[str], shell: Dict) -> None:
    """Apply a cd, export, unset or declaration, or a command made only of assignments (program ''), to the commands
    after it."""
    if program == 'cd':
        shell['cwd'] = _transfer_cd(args, cwd)
    elif program == 'unset':
        _transfer_unset(args, shell)
    elif program in _TRANSFER_DECLARES:
        _transfer_declare(program, args, shell)
    else:
        _transfer_assign(program, args, shell)


def _transfer_assign(program: str, args: List[str], shell: Dict) -> None:
    """Apply an export, or a command made only of assignments (program '')."""
    for arg in (a for a in args if not a.startswith('-')):
        name, eq, value = arg.partition('=')
        if name.endswith('+'):
            _transfer_forget(shell, {name[:-1]} - {''})  # bash appends, and zsh rejects it
        elif program == 'export':
            shell['vars'][name] = (value if eq else _transfer_variable(shell, name)[0], '-n' not in args)
        else:
            shell['vars'][name] = (value, _transfer_variable(shell, name)[1])  # an exported one stays exported


def _transfer_unset(args: List[str], shell: Dict) -> None:
    """unset -f removes functions, and -v or a bare name variables with their export. A bare name with no variable set
    removes the function in bash but not zsh, so it may or may not remain, as after an unset -f that may not run."""
    sure = shell['pipe']['runs'] is True and not _transfer_doubt(shell['pipe'])
    for name in (a for a in args if not a.startswith('-')):
        value = _transfer_variable(shell, name)[0]
        known = shell['functions'].get(name)
        if '-f' not in args:
            shell['vars'][name] = (None, False)
        if known and ('-f' in args or ('-v' not in args and (value is None or _TRANSFER_SUBSTITUTED in value))):
            if sure and '-f' in args:
                del shell['functions'][name]
            else:
                shell['functions'][name] = known[0], True


def _transfer_declare(program: str, args: List[str], shell: Dict) -> None:
    """declare or typeset outside a function set NAME=value, exporting with -x and not with +x. Other options, +=,
    local, readonly, and declarations in a function (bash scopes them, zsh may not) leave the names unknown."""
    on = off = ''
    while args and args[0][:1] in '-+' and len(args[0]) > 1:
        option, args = args[0], args[1:]
        if option == '--':
            break
        on, off = (on + option[1:], off) if option[0] == '-' else (on, off + option[1:])
    plain = program in ('declare', 'typeset') and shell['floor'] is None and set(on + off) <= {'x'} and not (on and off)
    for name, eq, value in (arg.partition('=') for arg in args):
        if not plain or name.endswith('+'):
            _transfer_forget(shell, {name.rstrip('+')} - {''})
            continue
        current, exported = _transfer_variable(shell, name)
        shell['vars'][name] = (value if eq else current, True if on else False if off else exported)


def _transfer_shell_string(program: str, args: List[str]) -> Optional[str]:
    """The command string `sh -c` or `powershell -Command` runs; None for any other command."""
    if program in _TRANSFER_SHELLS:
        has_c, i = False, 0
        while i < len(args):
            arg, i = args[i], i + 1
            if arg in ('-o', '+o', '-O', '+O'):
                i += 1
            elif arg[:1] in '-+' and not arg.startswith('--') and len(arg) > 1:
                has_c = has_c or 'c' in arg
            elif not arg.startswith('--'):
                return arg if has_c else None
    if program in _TRANSFER_POWERSHELLS:
        for i, arg in enumerate(args):
            name = arg[1:].lower() if arg[:1] in '-/' else ''
            if name == 'c' or (len(name) >= 3 and 'command'.startswith(name)):
                return ' '.join(args[i + 1:])
    return None


def _transfer_call(known: tuple, layer: tuple, subshell: Optional[bool], shell: Dict, depth: int) -> None:
    """Run a function's body (known: body, whether it may not exist) where called, in a subshell when piped or
    backgrounded; one that may not run, or a pipeline's last command (subshell None), leaves its changes unknown."""
    body, maybe = known
    if depth >= _TRANSFER_MAX_DEPTH or shell['calls_left'] <= 0:
        shell['status'] = None
        return
    shell['calls_left'] -= 1
    first, state = len(shell['calls']), _transfer_snapshot(shell)
    outer, outside = (shell['pipe'], shell['start'], shell['floor']), _transfer_liveness(shell)
    shell['floor'] = len(shell['frames'])
    _transfer_items(body, shell, depth + 1)
    shell['pipe'], shell['start'], shell['floor'] = outer
    _transfer_wrap(shell, first, layer)
    if subshell:
        _transfer_restore(shell, state, True)
        _transfer_revive(outside, shell)
    elif subshell is None or maybe or _transfer_doubt(shell['pipe']):
        _transfer_unsure(shell, state)
        _transfer_soften(outside, shell)


def _transfer_simple(words: List[str], before: str, after: str, io: list, shell: Dict, depth: int) -> List[tuple]:
    """The calls one simple command makes, after applying its cd, export, unset or declaration; a defined function runs
    its body with the command's words as its positional parameters."""
    layer = (before == '|', after == '|', tuple(io))
    substituted, words = any(_TRANSFER_SUBSTITUTED in w for w in words), _transfer_appends(words, shell)
    name = next((w for w in words if not _transfer_is_assignment(w)), None)
    # A piped or backgrounded command runs in a subshell; the last command of a pipeline (None) does in bash, but runs
    # in this shell in zsh.
    subshell = True if after in ('|', '&') else None if before == '|' else False
    known = shell['functions'].get(name)
    if known:
        outer, shell['args'] = shell['args'], words[words.index(name) + 1:]
        _transfer_call(known, layer, subshell, shell, depth)  # looked up first: the body may remove the function
        shell['args'] = outer
        if not known[1]:
            return []
        # A function that may not have been defined may leave the name to the program.
    program, args, env, cwd, other_user = _transfer_program(words, shell['cwd'])
    env = env if _TRANSFER_CLEAN_ENV in env else {**_transfer_exported(shell), **env}
    # A command of nothing but assignments sets them in the shell, and succeeds unless a substitution in it fails.
    assigns = program is None and all(_transfer_is_assignment(w) for w in words)
    status = shell['status']
    shell['status'] = 0 if program in ('true', ':', 'break', 'continue') else 1 if program == 'false' else \
        _transfer_arithmetic(words[0]) if len(words) == 1 else None
    if assigns and not substituted:
        shell['status'] = 0
    if program == 'cd' and _transfer_cd_rejected(args):
        shell['status'] = 1
    if program in ('set', 'shift'):
        shell['args'] = None  # the positional parameters may have changed
    if program in ('return', 'exit'):
        # Either leaves the status it is given, or else the one before it; a return outside a function fails.
        shell['status'] = status if not args else int(int(args[0]) % 256 > 0) if args[0].isdigit() else None
        if program == 'return' and shell['floor'] is None:
            shell['status'] = 1
    if program in ('return', 'exit', 'break', 'continue'):
        _transfer_jump_from(program, args, subshell, shell)
    if program in ('cd', 'export', 'unset') + _TRANSFER_DECLARES or assigns:
        # A pipeline's commands run in subshells, so their cd and variables end with them, but for the last in zsh.
        if (before != '|' and after != '|') or subshell is None:
            saved = _transfer_snapshot(shell)
            _transfer_builtin(program or '', words if assigns else args, cwd, shell)
            if subshell is None:
                _transfer_unsure(shell, saved)
            elif _transfer_doubt(shell['pipe']):
                _transfer_forget(shell, _transfer_changes(shell, saved))  # it may not have run
        _transfer_replace(words, subshell, shell)
        return []
    inner = _transfer_shell_string(program, args) if program else None
    if inner is not None:
        # The string's commands run as the wrapper's user and read its stdin and write its stdout.
        # Its words after the string are $0 and then the positional parameters.
        positional = args[args.index(inner) + 2:] if program in _TRANSFER_SHELLS else []
        calls = _transfer_walk(_TransferLexer(inner, depth + 1).read() or [], cwd, env, depth + 1, positional)
        calls = [call[:4] + (call[4] or other_user, call[5] + (layer,)) for call in calls]
    else:
        calls = [(program, args, env, cwd, other_user, (layer,))] if program else []
    if program:
        _transfer_replace(words, subshell, shell)
    return calls


def _transfer_replace(words: List[str], subshell: Optional[bool], shell: Dict) -> None:
    """After a command run through exec, which replaces the shell, nothing runs: exec itself, or behind time or
    builtin, and only maybe behind command, which bash runs as the builtin and zsh looks up as a program."""
    rest, behind_command = [w for w in words if not _transfer_is_assignment(w)], False
    while rest and rest[0] in ('command', 'builtin', 'time', '-p', '--'):
        behind_command, rest = behind_command or rest[0] == 'command', rest[1:]
    if len(rest) > 1 and rest[0] == 'exec':
        _transfer_jump_from('exit', [], subshell if not behind_command or subshell is True else None, shell)


def _transfer_jump_from(program: str, args: List[str], subshell: Optional[bool], shell: Dict) -> None:
    """A return, exit, break or continue where it runs: nothing beyond a subshell, and only maybe from the last
    command of a pipeline (subshell None), which ends its subshell in bash but runs in this shell in zsh."""
    if subshell is True:
        return
    outside = _transfer_liveness(shell)
    _transfer_jump(program, args, shell)
    if subshell is None:
        _transfer_soften(outside, shell)


def _transfer_command(item: tuple, shell: Dict, depth: int) -> List[tuple]:
    """The calls one simple command makes, if it runs or may run, after the reserved words that lead it."""
    _, words, before, after, io = item
    if words[0] not in ('}', 'fi', 'done'):
        _transfer_begin(before, shell)
    shell['opening_pipe'] = before == '|'
    while words and words[0] in _TRANSFER_KEYWORDS:
        # A body's first command is reached by its compound's pipe, which its own redirections then override.
        before = before if words[0] in ('!', 'function') else ''
        words = _transfer_keyword(words, item, shell)
    shell.pop('opening_pipe', None)
    closed = shell.pop('closed', None)
    if closed:
        _transfer_close(closed, (closed['piped_in'], after == '|', tuple(io)), shell, after == '&')
    calls = []
    if words:
        shell['pipe']['leading'] = False
        if shell['pipe']['runs'] is not False:
            calls = _transfer_simple(words, before, after, io, shell, depth)
    if words or closed:
        _transfer_finish(after, shell)
    return calls


def _transfer_items(items: List[tuple], shell: Dict, depth: int) -> None:
    """Walk items in this shell, adding each call that runs or may run to its calls."""
    outer = shell['items'], shell['at'], shell['ends']
    shell['items'], shell['ends'] = items, _transfer_ends(items)
    for at, item in enumerate(items):
        shell['at'] = at
        if item[0] == 'cmd':
            shell['calls'] += _transfer_command(item, shell, depth)
        else:
            _transfer_marker(item, shell)
    shell['items'], shell['at'], shell['ends'] = outer


def _transfer_ends(items: List[tuple]) -> Dict:
    """Where each compound in items ends, (operator after it, item), by where it opens (item, word): one piped or
    backgrounded at its end is a subshell, which no jump inside it leaves."""
    ends, opened = {}, []
    for at, (kind, words, _, after, _) in enumerate(items):
        if kind in ('(', '$(', 'case'):
            opened.append((at, 0))
        elif kind in (')', 'esac') and opened:
            ends[opened.pop()] = after, at
        k = 0
        while kind == 'cmd' and k < len(words) and words[k] in _TRANSFER_KEYWORDS:
            if words[k] in ('}', 'fi', 'done') and opened:
                ends[opened.pop()] = (after if k == len(words) - 1 else ''), at
            elif words[k] in ('{', 'if') + _TRANSFER_LOOPS:
                opened.append((at, k))
                if words[k] in ('for', 'select'):
                    break  # the rest is its header
            k += 2 if words[k] == 'function' else 1
    return ends


def _transfer_ends_piped(head: tuple, shell: Dict) -> bool:
    """Whether the compound opening at head, in the item being read, is piped or backgrounded at its end."""
    item = shell['items'][shell['at']] if shell['at'] < len(shell['items']) else head
    k = len(item[1]) - len(head[1]) if head[0] == 'cmd' and item[0] == 'cmd' else 0
    return shell['ends'].get((shell['at'], k), ('', None))[0] in ('|', '&')


def _transfer_walk(items: List[tuple], cwd: Optional[str], exported: Dict, depth: int,
                   positional: List[str] = ()) -> List[tuple]:
    """Each simple command that runs or may run, as (program, args, env, folder, as another user, its (piped in, piped
    out, redirections) layers from its own outward); skipped branches and uncalled function bodies are left out."""
    return _transfer_shell(items, cwd, exported, depth, positional)['calls']


def _transfer_next_cwd(command: Optional[str], cwd: Optional[str]) -> Optional[str]:
    """The folder a persistent shell is in after a command, where the next command's lookups start: moved only by a cd
    that surely ran, and None when that is not known. Never raises."""
    try:
        if not isinstance(command, str) or not command.strip():
            return cwd
        items = _TransferLexer(command).read()
        return _transfer_shell(items, cwd, {}, 0)['cwd'] if items is not None else None
    except Exception:
        return None


def _transfer_shell(items: List[tuple], cwd: Optional[str], exported: Dict, depth: int,
                    positional: List[str] = ()) -> Dict:
    """The shell after walking items: the calls made, and the folder, variables and functions it ends with. A fresh
    shell has no positional parameters, and one a string runs gets the words after it."""
    variables = {name: (value, True) for name, value in exported.items()}
    shell = {'cwd': cwd, 'vars': variables, 'status': None, 'frames': [], 'function': None,
             'start': (cwd, dict(variables), {}), 'calls': [], 'functions': {}, 'calls_left': _TRANSFER_MAX_CALLS,
             'items': items, 'at': 0, 'live': True, 'floor': None, 'args': list(positional), 'ends': {}}
    _transfer_pipeline('', True, shell)
    _transfer_items(items, shell, depth)
    return shell


def _transfer_short_option(arg: str, args: List[str], i: int, value_flags: frozenset, seen: set) -> tuple:
    """(the option in a cluster such as -sSLo that takes a value, its value, the next index); switches go to seen."""
    for j, flag in enumerate(arg[1:], start=2):
        if flag in value_flags:
            value = arg[j:]
            if not value and i < len(args):
                value, i = args[i], i + 1
            return '-' + flag, value, i
        seen.add('-' + flag)
    return arg, None, i


def _transfer_url_host(url: str) -> str:
    """The host a URL names, with any login, port and path dropped; empty when it names none."""
    from urllib.parse import urlsplit
    try:
        text = url.strip()
        parts = urlsplit(text if '://' in text else 'http://' + text)
        host = parts.hostname or ''
        if '[' in parts.netloc and not _transfer_ip(host):
            return ''  # brackets hold nothing but an IPv6 address, which older Pythons do not check
        if host.isascii():
            return host
        import unicodedata
        # curl (libidn2) and the URL standard dial each non-ASCII label in punycode, keeping ß and a final sigma.
        return '.'.join(label if label.isascii() else 'xn--' + label.encode('punycode').decode('ascii')
                        for label in unicodedata.normalize('NFC', host).split('.'))
    except ValueError:
        return ''


def _transfer_curl(args: List[str]) -> List[tuple]:
    """curl's URLs: sent to when their request uploads or sends data, else fetched from. Each --next starts a request,
    --resolve pins carry on, and globs reach every host unless --globoff; a rejected glob stops curl."""
    endpoints, pins, i = [], [], 0
    while True:
        urls, sends, overrides, globoff, i = _transfer_curl_request(args, i)
        pins += [value for option, value in overrides if option == '--resolve']
        connects = [value for option, value in overrides if option == '--connect-to']
        for url in urls:
            expanded = [url] if globoff else _transfer_curl_glob(url)
            if expanded is None:
                return endpoints
            endpoints += [('out' if sends else 'in', _transfer_url_host(one), None,
                           _transfer_curl_dial(one, connects, pins)) for one in expanded]
        if i >= len(args):
            return endpoints


def _transfer_curl_glob(url: str) -> Optional[List[str]]:
    """The URLs curl makes of one by expanding its {a,b} sets and [1-3] or [a-c] ranges, as far as they change the
    host; None for a glob curl rejects."""
    pieces = _transfer_glob_pieces(url)
    if pieces is None:
        return None
    urls = ['']
    for text, values in pieces:
        urls = [u + text for u in urls]
        # Past the host a glob only changes the path, so one of its values stands for all.
        in_host = not all(re.search(r'[/?#]', u.split('://', 1)[-1]) for u in urls)
        urls = [u + v for u in urls for v in (values if in_host else values[:1])][:_TRANSFER_MAX_WORDS]
    return urls


def _transfer_glob_pieces(url: str) -> Optional[List[tuple]]:
    """A curl URL as (literal text, the values of the glob after it) pieces, read as curl reads {a,b} sets, [1-3] or
    [a-c] ranges with an optional :step, a bracketed IPv6 address and backslash escapes; None for a glob it rejects."""
    pieces, text, i = [], '', 0
    while i < len(url):
        ch = url[i]
        if ch == '\\' and url[i + 1:i + 2] in ('{', '}', '[', ']', ','):
            text, i = text + url[i + 1], i + 2
            continue
        if ch in '}]':
            return None  # a close with no open
        if ch not in '{[':
            text, i = text + ch, i + 1
            continue
        end = i + 1
        while end < len(url) and url[end] != {'{': '}', '[': ']'}[ch]:
            if url[end] == ch:
                return None  # a nested open
            end += 2 if url[end] == '\\' else 1
        if end >= len(url):
            return None
        body = url[i + 1:end]
        if ch == '[' and ':' in body and _transfer_ip(body):
            text += url[i:end + 1]  # an IPv6 address, not a range
        else:
            values = [re.sub(r'\\(.)', r'\1', v) for v in re.split(r'(?<!\\),', body)] if ch == '{' else \
                _transfer_range(body)
            if values is None:
                return None
            pieces.append((text, values))
            text = ''
        i = end + 1
    return pieces + [(text, [''])]


def _transfer_range(body: str) -> Optional[List[str]]:
    """The values a curl [1-3], [01-10:2] or [a-c] range takes; None for one curl rejects."""
    match = re.match(r'^(?:(\d+)-(\d+)|([a-z])-([a-z])|([A-Z])-([A-Z]))(?::(\d+))?$', body)
    step = int(match.group(7) or 1) if match else 0
    if not step:
        return None
    if match.group(1) is not None:
        first, last = match.group(1), match.group(2)
        width = len(first) if first.startswith('0') else 0
        values = [str(n).zfill(width) for n in range(int(first), int(last) + 1, step)[:_TRANSFER_MAX_WORDS]]
    else:
        first, last = match.group(3) or match.group(5), match.group(4) or match.group(6)
        values = [chr(c) for c in range(ord(first), ord(last) + 1, step)]
    return values or None


def _transfer_ip(text: str):
    """The IP address a string holds, brackets allowed; None when it holds none."""
    import ipaddress
    try:
        return ipaddress.ip_address(text.strip().strip('[]'))
    except ValueError:
        return None


def _transfer_curl_dial(url: str, connects: List[str], pins: List[str]) -> Optional[tuple]:
    """(the host a URL's request connects to, the addresses pinned for it), as curl applies the first matching
    --connect-to and then the last --resolve pin for that host and port, a named host before *; None when neither."""
    from urllib.parse import urlsplit
    host = _transfer_url_host(url)
    try:
        parts = urlsplit(url.strip() if '://' in url else 'http://' + url.strip())
        port = str(parts.port or _TRANSFER_DEFAULT_PORTS.get(parts.scheme.lower(), ''))
    except ValueError:
        return None
    target = (host, port)
    for rule in connects:
        fields = re.split(r':(?![^\[]*\])', rule)  # colons inside [ ] belong to an IPv6 address
        if len(fields) == 4 and _transfer_clean_host(fields[0]) in ('', host) and fields[1] in ('', port):
            target = (_transfer_clean_host(fields[2]) or host, fields[3] or port)
            break
    pinned = {}
    for pin in pins:
        fields = re.split(r':(?![^\[]*\])', pin.lstrip('+-'))
        key = ('*' if fields[0] == '*' else _transfer_clean_host(fields[0]), fields[1] if len(fields) > 1 else '')
        pinned.pop(key, None)
        if not pin.startswith('-') and len(fields) > 2:
            pinned[key] = [ip for ip in map(_transfer_ip, ':'.join(fields[2:]).split(',')) if ip]
    addresses = pinned.get(target) or pinned.get(('*', target[1])) or []
    if target[0] == host and not addresses:
        return None
    return (str(addresses[0]) if addresses else target[0]), addresses


def _transfer_curl_request(args: List[str], i: int) -> tuple:
    """(the URLs of the curl request at args[i], whether it sends data, its --resolve and --connect-to values, whether
    it turns globbing off, where the next request starts); a --next that is an option's value does not end it."""
    urls, flags, method, overrides = [], set(), '', []
    while i < len(args):
        arg, value, i = args[i], None, i + 1
        if arg in ('-:', '--next'):
            break
        if arg.startswith('--'):
            # curl reads --name=value as --name value, and --expand-name as --name with variables expanded.
            arg, eq, attached = arg.partition('=')
            arg = '--' + arg[len('--expand-'):] if arg.startswith('--expand-') else arg
            if eq:
                value = attached
            elif arg in _TRANSFER_CURL_LONG_VALUES and i < len(args):
                value, i = args[i], i + 1
        elif arg.startswith('-') and len(arg) > 1:
            arg, value, i = _transfer_short_option(arg, args, i, _TRANSFER_CURL_SHORT_VALUES, flags)
        else:
            urls.append(arg)
            continue
        flags.add(arg)
        if arg == '--url' and value:
            urls.append(value)
        elif arg in ('-X', '--request') and value:
            method = value.upper()
        elif arg in ('--resolve', '--connect-to') and value:
            overrides.append((arg, value))
    sends = flags & _TRANSFER_CURL_UPLOADS or method in _TRANSFER_SENDING_METHODS or \
        (flags & _TRANSFER_CURL_BODIES and not flags & {'-G', '--get'})
    return urls, bool(sends), overrides, bool(flags & {'-g', '--globoff'}), i


def _transfer_wget(args: List[str]) -> List[tuple]:
    """wget's URLs: sent to when it posts data or a file; else fetched from."""
    urls, flags, i = [], set(), 0
    while i < len(args):
        arg, i = args[i], i + 1
        if arg.startswith('--'):
            name, eq, _ = arg.partition('=')
            if not eq and name in _TRANSFER_WGET_LONG_VALUES:
                i += 1
            flags.add(name)
        elif arg.startswith('-') and len(arg) > 1:
            flag, _, i = _transfer_short_option(arg, args, i, _TRANSFER_WGET_SHORT_VALUES, flags)
            flags.add(flag)
        else:
            urls.append(arg)
    direction = 'out' if flags & _TRANSFER_WGET_UPLOADS else 'in'
    return [(direction, _transfer_url_host(url), None) for url in urls]


def _transfer_ssh_args(program: str, args: List[str]) -> tuple:
    """(operands, the options that change which host or user ssh -G resolves) for ssh, scp or sftp."""
    passthrough, operands, i = [], [], 0
    value_flags = _TRANSFER_SSH_VALUE_FLAGS[program]
    # scp's -l is a bandwidth limit, not a login.
    identity_flags = ('l', 'o', 'F', 'J', 'p') if program == 'ssh' else ('o', 'F', 'J', 'P')
    while i < len(args):
        arg, i = args[i], i + 1
        # ssh reads options before and right after its destination; the first other word starts the remote command.
        reading_options = not operands or (program == 'ssh' and len(operands) == 1)
        if not reading_options or not arg.startswith('-') or arg == '-':
            operands.append(arg)
            continue
        if arg == '--':
            operands.extend(args[i:])
            break
        flag, value, i = _transfer_short_option(arg, args, i, value_flags, set())
        option = re.split(r'[\s=]', (value or '').strip(), maxsplit=1)[0].lower()
        if value and flag[1:] in identity_flags and (flag != '-o' or option in _TRANSFER_SSH_IDENTITY_OPTIONS):
            passthrough += ['-p' if flag == '-P' else flag, value]
    return operands, passthrough


def _transfer_rsync_args(args: List[str], env: Dict) -> tuple:
    """(operands, ssh options for its remote shell, or None when that shell isn't ssh) for rsync."""
    shell, operands, i = _transfer_env(env, 'RSYNC_RSH'), [], 0
    while i < len(args):
        arg, i = args[i], i + 1
        if arg == '--rsh' and i < len(args):
            shell, i = args[i], i + 1
        elif arg.startswith('--rsh='):
            shell = arg.split('=', 1)[1]
        elif arg in _TRANSFER_RSYNC_VALUE_OPTIONS:
            i += 1
        elif arg.startswith('-') and not arg.startswith('--'):
            flag, value, i = _transfer_short_option(arg, args, i, _TRANSFER_SSH_VALUE_FLAGS['rsync'], set())
            shell = value if flag == '-e' else shell
        elif not arg.startswith('-'):
            operands.append(arg)
    if not shell:
        return operands, []
    words = _transfer_words(shell)
    if not words or _transfer_program_name(words[0]) != 'ssh':
        return operands, None
    return operands, _transfer_ssh_args('ssh', words[1:] + ['-'])[1]


def _transfer_remote(arg: str) -> Optional[tuple]:
    """(destination, port, whether ssh carries it) for a remote copy operand; None for a local path."""
    uri = re.match(r'^(scp|sftp|rsync)://(?:([^@/;:]+)(?:[;:][^@/]*)?@)?(\[[^\]/]+\]|[^/:\[\]@]+)(?::(\d+))?(?=/|$)', arg)
    if uri:
        # A URI names the login and port itself; anything after the user (a password) is dropped.
        destination = uri.group(2) + '@' + uri.group(3) if uri.group(2) else uri.group(3)
        return destination, uri.group(4), uri.group(1) != 'rsync'
    if '://' in arg:
        return None
    # A bracketed host may hold an IPv6 address; a second colon after the host marks an rsync daemon.
    match = re.match(r'^((?:[^@/:\[\]]+@)?(?:\[[^\]/]+\]|[^/:\[\]]+)):(:)?', arg)
    # A drive letter is a local path only on Windows; elsewhere scp dials host C.
    if not match or (os.name == 'nt' and re.match(r'^[A-Za-z]:[\\/]', arg)):
        return None
    return match.group(1), None, not match.group(2)


def _transfer_copy(program: str, args: List[str], env: Dict) -> List[tuple]:
    """Each remote side of an scp, sftp or rsync copy: fetched from as a source, sent to as the target."""
    operands, options = _transfer_rsync_args(args, env) if program == 'rsync' else _transfer_ssh_args(program, args)
    remotes = [_transfer_remote(op) for op in operands]
    if program == 'sftp' and operands:
        # sftp takes one destination: a bare host opens a session, a host:path or URI fetches that path.
        plain = re.match(r'^(?:[^@/:\[\]]+@)?(?:\[[^\]/]+\]|[^/:\[\]]+)$', operands[0])
        remotes = [((operands[0], None, True) if plain else remotes[0], '' if plain else 'in')]
    elif len(remotes) == 1:
        remotes = [(remotes[0], 'in' if program == 'rsync' else '')]  # rsync lists a lone remote
    else:
        remotes = [(r, 'in') for r in remotes[:-1]] + [(remotes[-1], 'out')] if remotes else []
    endpoints = []
    for remote, direction in remotes:
        if remote:
            destination, port, via_ssh = remote
            ssh_options = (options + (['-p', port] if port else [])) if via_ssh and options is not None else None
            endpoints.append((direction, destination, ssh_options))
    return endpoints


def _transfer_ssh_pipe(args: List[str], sends: bool, receives: bool) -> List[tuple]:
    """ssh used as a pipe: sent to when its stdin is fed in, fetched from when its stdout is kept."""
    operands, options = _transfer_ssh_args('ssh', args)
    if not operands:
        return []
    return [(d, operands[0], options) for d, used in (('out', sends), ('in', receives)) if used]


def _transfer_netcat(args: List[str], sends: bool) -> List[tuple]:
    """nc's host: sent to when its stdin is fed in; a listener reaches no host."""
    positional, flags, i = [], set(), 0
    while i < len(args):
        arg, i = args[i], i + 1
        if arg.startswith('--'):
            if '=' not in arg and arg in _TRANSFER_NC_LONG_VALUES:
                i += 1
            flags.add(arg)
        elif arg.startswith('-') and len(arg) > 1:
            flag, _, i = _transfer_short_option(arg, args, i, _TRANSFER_NC_SHORT_VALUES, flags)
            flags.add(flag)
        else:
            positional.append(arg)
    if not positional or flags & {'-l', '--listen', '-U', '--unixsock'}:
        return []
    return [('out' if sends else '', positional[0], None)]


def _transfer_socat(args: List[str], sends: bool) -> List[tuple]:
    """The hosts socat's two addresses connect to: sent to when its stdin is fed in."""
    addresses, i = [], 0
    while i < len(args):
        arg, i = args[i], i + 1
        if arg in _TRANSFER_SOCAT_VALUE_OPTIONS:
            i += 1
        elif arg == '-' or not arg.startswith('-'):
            addresses.append(arg)
    endpoints = []
    for address in addresses[:2]:
        match = re.match(r'^(?:(?:tcp|udp|sctp|dccp|openssl|ssl)[46]?(?:-connect|-sendto|-datagram)?|'
                         r'(?:proxy|socks4a?|socks5):[^:,]+):(\[[^\]]+\]|[^:,]+):', address, re.IGNORECASE)
        if match:
            endpoints.append(('out' if sends else '', match.group(1), None))
    return endpoints


def _transfer_httpie_item(item: str) -> bool:
    """Whether an HTTPie request item is data (=, :=, @, =@, :=@) rather than a header (:) or a query (==)."""
    match = re.match(r'^(?:\\.|[^\\=:@])*(:=@|=@|:=|==|=|@|:)', item)
    return bool(match) and match.group(1) not in ('==', ':')


def _transfer_httpie(args: List[str], sends: bool) -> List[tuple]:
    """HTTPie's URL: sent to when the request carries data or a sending method; else fetched from."""
    positional, flags, i = [], set(), 0
    while i < len(args):
        arg, i = args[i], i + 1
        if arg.startswith('--'):
            name, eq, _ = arg.partition('=')
            if not eq and name in _TRANSFER_HTTPIE_LONG_VALUES:
                i += 1
            flags.add(name)
        elif arg.startswith('-') and len(arg) > 1:
            flag, _, i = _transfer_short_option(arg, args, i, _TRANSFER_HTTPIE_SHORT_VALUES, flags)
            flags.add(flag)
        else:
            positional.append(arg)
    if not positional or '--offline' in flags:
        return []
    method = positional[0].upper() if len(positional) > 1 and re.match(r'^[A-Za-z]+$', positional[0]) else ''
    url, items = (positional[1], positional[2:]) if method else (positional[0], positional[1:])
    reads_stdin = sends and not flags & {'-I', '--ignore-stdin'}
    out = method in _TRANSFER_SENDING_METHODS or '--raw' in flags or reads_stdin or any(map(_transfer_httpie_item, items))
    return [('out' if out else 'in', 'localhost' if url.startswith(':') else _transfer_url_host(url), None)]


def _transfer_web_cmdlet(args: List[str], sends: bool) -> List[tuple]:
    """Invoke-WebRequest's or Invoke-RestMethod's URI: sent to with a body, file, form, piped input or sending
    method; else fetched from."""
    uri, out, positional, i = '', sends, [], 0
    while i < len(args):
        arg, i = args[i], i + 1
        if not re.match(r'^-[A-Za-z]', arg):
            positional.append(arg)
            continue
        name, colon, value = arg[1:].partition(':')
        name = name.lower()
        if not colon and name not in _TRANSFER_PS_SWITCHES and i < len(args):
            value, i = args[i], i + 1
        if name == 'uri':
            uri = value
        elif name in ('body', 'infile', 'form'):
            out = True
        elif name in ('method', 'custommethod') and value.upper() in _TRANSFER_SENDING_METHODS:
            out = True
    uri = uri or (positional[0] if positional else '')
    return [('out' if out else 'in', _transfer_url_host(uri), None)] if uri else []


def _transfer_stdio(layers: tuple) -> tuple:
    """(whether a call's stdin is fed from a file or pipe, whether its stdout goes to one), from the pipes and
    redirections around it applied as the shell does: the outermost first, each one's pipes before its redirections."""
    fds = {0: 'null', 1: 'null', 2: 'null'}  # the agent's own stdin and stdout carry no transfer
    for piped_in, piped_out, redirections in reversed(layers):
        if piped_in:
            fds[0] = 'pipe'
        if piped_out:
            fds[1] = 'pipe'
        for fd, value in redirections:
            fds[fd] = fds.get(value, 'null') if isinstance(value, int) else value
    return fds[0] in ('file', 'pipe'), fds[1] in ('file', 'pipe')


def _transfer_parse(program: str, args: List[str], env: Dict, other_user: bool, layers: tuple) -> Optional[tuple]:
    """(tool, [(direction, destination, ssh -G options or None[, curl's dial])]) for a transfer command; None for any
    other."""
    tool, (sends, keeps) = _TRANSFER_TOOLS.get(program), _transfer_stdio(layers)
    if tool in ('scp', 'sftp', 'rsync'):
        raw = _transfer_copy(tool, args, env)
    elif tool == 'ssh':
        raw = _transfer_ssh_pipe(args, sends, keeps)
    elif tool in ('curl', 'wget'):
        raw = _transfer_curl(args) if tool == 'curl' else _transfer_wget(args)
    elif tool in ('nc', 'socat'):
        raw = _transfer_netcat(args, sends) if tool == 'nc' else _transfer_socat(args, sends)
    elif tool in ('httpie', 'powershell'):
        raw = _transfer_httpie(args, sends) if tool == 'httpie' else _transfer_web_cmdlet(args, sends)
    else:
        return None
    # Under sudo, ssh runs as that user, with that user's config, which this can't read.
    return tool, [(e[0], e[1], None if other_user else e[2]) + e[3:] for e in raw]


def _transfer_clean_host(host: str) -> str:
    """A host name or address in lower case without brackets; empty when it isn't one."""
    import ipaddress
    host = host.strip().lower()
    host = host[1:-1] if host.startswith('[') and host.endswith(']') else host
    if re.match(r'^[a-z0-9_][a-z0-9_.-]{0,252}$', host):
        return host
    try:
        return str(ipaddress.ip_address(host.split('%')[0])) if ':' in host else ''
    except ValueError:
        return ''


def _transfer_home() -> str:
    """The home folder ssh reads its config from: the account's own, which $HOME does not change."""
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    except Exception:
        return os.path.expanduser('~')


def _transfer_ssh_config_words(line: str) -> Optional[List[str]]:
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


def _transfer_match_execs(words: List[str]) -> bool:
    """Whether a Match line's criteria include exec, read as ssh reads them: each a keyword, negated with !, taking an
    argument after a space or = unless it is all, canonical or final."""
    i = 0
    while i < len(words):
        criterion, eq, _ = words[i].partition('=')
        criterion = criterion.lower().lstrip('!')
        if criterion == 'exec':
            return True
        i += 1 if eq or criterion in ('all', 'canonical', 'final') else 2
    return False


def _transfer_ssh_config_runs(lines: List[str], homes: List[str], bases: tuple, pending: List[tuple]) -> bool:
    """Whether config lines could run a command or a DNS lookup in ssh -G; their Includes are queued on pending,
    resolved against every home and folder ssh may use."""
    import glob
    for line in lines:
        words = _transfer_ssh_config_words(line)
        if words is None:
            return True
        if not words:
            continue
        keyword, _, attached = words[0].partition('=')
        # ssh allows `Keyword=value` and `Keyword =value`, so a leading = is only a separator.
        words = [w.lstrip('=') for w in ([attached] if attached else []) + words[1:] if w.lstrip('=')]
        if keyword.lower() == 'match' and _transfer_match_execs(words):
            return True
        if keyword.lower() == 'canonicalizehostname' and words and words[0].lower() in ('yes', 'always'):
            return True
        for pattern in (words if keyword.lower() == 'include' else []):
            if '$' in pattern or '%' in pattern:
                return True  # ssh may expand a variable or token here
            # ssh globs the path, and glob reads a backslash as escaping the next character.
            pattern = re.sub(r'\\(.)', r'\1', pattern)
            if pattern == '~' or pattern.startswith('~/'):
                paths = [home + pattern[1:] for home in homes]
            else:
                paths = [os.path.join(base, os.path.expanduser(pattern)) for base in bases]
            for path in paths:
                pending += [(found, bases) for found in sorted(glob.glob(path))]
    return False


def _transfer_ssh_config_unsafe(passthrough: List[str], cwd: Optional[str]) -> bool:
    """Whether ssh -G could run a command or a DNS lookup while reading its configs, or reads a file this can't check."""
    import stat
    home = _transfer_home()
    # ssh reads ~/.ssh/config from the account's home but expands an Include's ~ and relative path with $HOME.
    homes = list(dict.fromkeys([home, os.path.expanduser('~')]))
    user_bases = tuple(os.path.join(h, '.ssh') for h in homes)
    config = None
    for flag, value in zip(passthrough[::2], passthrough[1::2]):
        if flag == '-F':
            config = os.path.expanduser(value)
    if config == 'none':
        return False
    if config is None:
        # Whichever ssh is on PATH reads its own system config, so every one that exists is checked.
        system = ['/etc/ssh', '/opt/homebrew/etc/ssh', '/usr/local/etc/ssh',
                  os.path.join(os.environ.get('PROGRAMDATA') or '/nonexistent', 'ssh')]
        pending = [(os.path.join(home, '.ssh', 'config'), user_bases)] + \
            [(os.path.join(d, 'ssh_config'), (d,)) for d in system]
    elif os.path.isabs(config) or (cwd and os.path.isdir(cwd)):
        pending = [(os.path.join(cwd or '', config), user_bases)]
    else:
        return True
    seen = set()
    while pending:
        path, bases = pending.pop()
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
        if len(seen) > _TRANSFER_SSH_CONFIG_MAX_FILES or not stat.S_ISREG(info.st_mode) or \
                info.st_size > _TRANSFER_SSH_CONFIG_MAX_BYTES:
            return True
        try:
            with open(path, errors='ignore') as handle:
                lines = handle.read().splitlines()
        except OSError:
            return True
        if _transfer_ssh_config_runs(lines, homes, bases, pending):
            return True
    return False


def _transfer_ssh_hostname(destination: str, options: List[str], cwd: Optional[str], state: Dict) -> str:
    """The host ssh connects to for a destination, read from ssh -G; empty when it can't be looked up safely."""
    # The shell expands a leading ~ in an -F path before ssh sees it.
    options = [os.path.expanduser(v) if i and options[i - 1] == '-F' else v for i, v in enumerate(options)]
    key = ('ssh', destination, tuple(options), cwd)
    if key not in state:
        state[key] = ''
        # A destination starting with - would reach ssh -G as an option.
        if not destination.startswith('-') and not _transfer_ssh_config_unsafe(options, cwd):
            target = destination.replace('[', '').replace(']', '')
            out = _transfer_run(['ssh', '-G'] + options + ['--', target], cwd, state) or ''
            names = [line.split(None, 1)[1] for line in out.splitlines() if line.startswith('hostname ')]
            state[key] = _transfer_clean_host(names[0]) if names else ''
    return state[key]


def _transfer_kind(address) -> str:
    import ipaddress
    address = getattr(address, 'ipv4_mapped', None) or address
    for kind, networks in _TRANSFER_ADDRESS_KINDS:
        if any(address in ipaddress.ip_network(n) for n in networks):
            return kind
    return 'public'


def _transfer_resolve(host: str, timeout: float) -> list:
    """The host's addresses from the system resolver; [] when it fails or takes longer than timeout."""
    import ipaddress
    import socket
    import threading
    found = []

    def lookup():
        try:
            for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP):
                found.append(ipaddress.ip_address(info[4][0].split('%')[0]))
        except Exception:
            pass

    # A daemon thread, so a resolver that hangs past the timeout never holds the hook open.
    worker = threading.Thread(target=lookup, daemon=True)
    worker.start()
    worker.join(timeout)
    return [] if worker.is_alive() else list(found)


def _transfer_address(host: str, state: Dict) -> str:
    """The kind of address a host resolves to, looked up once per turn: public when any address is public, else
    the first address's kind; unresolved when the lookup fails or the turn's budget is spent."""
    import ipaddress
    key = ('address', host)
    if key not in state:
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            remaining = _transfer_deadline(state) - time.monotonic()
            addresses = _transfer_resolve(host, min(_TRANSFER_DNS_TIMEOUT, remaining)) if remaining > 0 else []
        state[key] = _transfer_address_kind(addresses)
    return state[key]


def _transfer_address_kind(addresses: list) -> str:
    """public when any address is public, else the first address's kind; unresolved when there is none."""
    kinds = [_transfer_kind(a) for a in addresses]
    return 'unresolved' if not kinds else 'public' if 'public' in kinds else kinds[0]


def _transfer_endpoint(raw: tuple, cwd: Optional[str], state: Dict) -> Optional[Dict]:
    """One place a transfer reached, from (direction, destination, ssh -G options or None[, curl's dial]): the host as
    written without a login, the host it connects to (ssh -G or curl's dial), and that host's address kind."""
    direction, destination, ssh_options = raw[:3]
    dial = raw[3] if len(raw) > 3 else None
    destination = destination.strip()
    host = _transfer_url_host(destination) if '://' in destination else destination.rsplit('@', 1)[-1]
    host = _transfer_clean_host(host)
    if not host:
        return None
    if dial:
        resolved, address = dial[0], _transfer_address_kind(dial[1]) if dial[1] else _transfer_address(dial[0], state)
    else:
        resolved = (_transfer_ssh_hostname(destination, ssh_options, cwd, state) if ssh_options is not None else '') \
            or host
        address = _transfer_address(resolved, state)
    endpoint = {'direction': direction, 'host': host, 'resolved_host': resolved, 'address': address}
    return {k: v for k, v in endpoint.items() if v}


def _transfer(command: Optional[str], cwd: Optional[str], state: Dict) -> Optional[Dict]:
    """Where a shell call's transfers went: the first transfer tool it ran and each place it sent to or fetched
    from; None when it ran no transfer. Never raises, and never takes longer than the turn's budget."""
    try:
        if not isinstance(command, str) or not command.strip():
            return None
        tool, endpoints = None, []
        for program, args, env, call_cwd, other_user, layers in \
                _transfer_walk(_TransferLexer(command).read() or [], cwd, {}, 0):
            found = _transfer_parse(program, args, env, other_user, layers)
            for raw in (found[1] if found else []):
                if len(endpoints) >= _TRANSFER_MAX_ENDPOINTS:
                    break
                endpoint = _transfer_endpoint(raw, call_cwd, state)
                if endpoint and endpoint not in endpoints:
                    endpoints.append(endpoint)
                    tool = tool or found[0]
        return {'tool': tool, 'endpoints': endpoints} if endpoints else None
    except Exception:
        return None


def _call_ts(stamp: Any) -> Optional[str]:
    """A log timestamp as ISO 8601 UTC to the second, such as 2026-10-22T16:40:00Z; None when it can't be read."""
    from datetime import datetime, timezone
    try:
        match = re.match(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})?$', str(stamp).strip())
        if not match:
            return None
        moment = datetime.fromisoformat(match.group(1) + ('+00:00' if match.group(2) in (None, 'Z') else match.group(2)))
        return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    except Exception:
        return None


def _with_call_ts(entry: Dict, stamp: Any) -> Dict:
    """The tool_use entry with the time its call was logged, when that time can be read."""
    ts = _call_ts(stamp)
    if ts:
        entry['ts'] = ts
    return entry
# --- end transfer resolution ---


def build_llm_exchange(events, api_key=None):
    """Build standard LLM exchange format from events."""
    messages = []
    assistant_tool_uses = []
    
    user_prompt = None
    assistant_response = None
    conversation_id = None
    model = None
    transfer_state = {}
    
    for log_entry in events:
        event = log_entry.get('event', {})
        hook_event_name = event.get('hook_event_name')
        stamp = log_entry.get('timestamp')
        
        if not conversation_id:
            conversation_id = event.get('conversation_id')
        
        if not model:
            model = event.get('model')
        
        if hook_event_name == 'beforeSubmitPrompt':
            user_prompt = event.get('prompt')
        
        elif hook_event_name == 'beforeReadFile':
            assistant_tool_uses.append(_with_call_ts({
                'type': hook_event_name,
                'file_path': event.get('file_path'),
                'content': event.get('content', ''),
                'attachments': event.get('attachments', [])
            }, stamp))
        
        elif hook_event_name == 'afterFileEdit':
            assistant_tool_uses.append(_with_call_ts({
                'type': hook_event_name,
                'file_path': event.get('file_path'),
                'edits': event.get('edits', [])
            }, stamp))
        
        elif hook_event_name == 'afterShellExecution':
            shell_entry = _with_call_ts({
                'type': hook_event_name,
                'command': event.get('command'),
                'output': event.get('output', '')
            }, stamp)
            transfer = _transfer(event.get('command'), event.get('cwd'), transfer_state)
            if transfer:
                shell_entry['transfer'] = transfer
            assistant_tool_uses.append(shell_entry)
        
        elif hook_event_name == 'afterMCPExecution':
            assistant_tool_uses.append(_with_call_ts({
                'type': hook_event_name,
                'tool_name': event.get('tool_name'),
                'tool_input': event.get('tool_input'),
                'result_json': event.get('result_json')
            }, stamp))
        
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