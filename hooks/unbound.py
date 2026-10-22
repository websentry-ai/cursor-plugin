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
    'nohup': ('', set(), 0),
}
# Reserved words that open, divide or close a compound command.
_TRANSFER_KEYWORDS = frozenset({'{', '}', 'if', 'then', 'elif', 'else', 'fi', 'while', 'until', 'for', 'select', 'do',
                                'done', '!', 'function'})
# Reserved words after which the next word still starts a command.
_TRANSFER_LEADING_WORDS = frozenset({'{', 'if', 'then', 'elif', 'else', 'while', 'until', 'do', '!'})
_TRANSFER_LOOPS = ('while', 'until', 'for', 'select')
_TRANSFER_SHELLS = frozenset({'sh', 'bash', 'zsh', 'dash', 'ksh', 'ash'})
_TRANSFER_POWERSHELLS = frozenset({'powershell', 'pwsh'})
# Set in a command's env by `env -i`: nothing is inherited from the hook's environment.
_TRANSFER_CLEAN_ENV = '\0clean'
# Stands in for a substitution's output inside a word, so no host is read from it.
_TRANSFER_SUBSTITUTED = '\0'
_TRANSFER_TOOLS = {
    'curl': 'curl', 'wget': 'wget', 'scp': 'scp', 'rsync': 'rsync', 'sftp': 'sftp', 'ssh': 'ssh',
    'nc': 'nc', 'ncat': 'nc', 'netcat': 'nc', 'socat': 'socat', 'http': 'httpie', 'https': 'httpie',
    'invoke-webrequest': 'powershell', 'invoke-restmethod': 'powershell', 'iwr': 'powershell', 'irm': 'powershell',
}
_TRANSFER_SENDING_METHODS = frozenset({'POST', 'PUT', 'PATCH'})
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
    """Reads a command the way the shell does into items, in the order they run: ('cmd', argv, operator before,
    operator after, redirections) for each simple command, and ('(' | '$(' | ')' | 'func' | 'case' | 'arm' | 'esac',
    None, operator before, '', None) around subshells, substitutions, function bodies and case arms. Redirections
    hold 'in' when stdin comes from a file or heredoc and 'out' when stdout goes to a file."""

    def __init__(self, text: str, depth: int = 0):
        self.text, self.depth, self.i, self.quote = text, depth, 0, None
        self.items, self.argv, self.word, self.in_word = [], [], '', False
        self.before, self.io, self.heredocs, self.case = '', set(), [], []

    def read(self) -> Optional[List[tuple]]:
        """The items, or None when the command can't be read the way the shell reads it."""
        if self.depth > _TRANSFER_MAX_DEPTH:
            return None
        while self.i < len(self.text):
            if not (self._quoted() if self.quote else self._unquoted()):
                return None
            self.i += 1
        if self.quote:
            return None
        self._end_command('')
        return self.items

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
            self.word, self.in_word = self.word + ch, True
        return True

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
        rest = self.text[self.i + 1:].lstrip(' \t')
        name = self.word if self.in_word else (self.argv[-1] if self.argv else '')
        head = self.argv if self.in_word else self.argv[:-1]
        leading = head[:-1] if head[-1:] == ['function'] else head
        if rest.startswith(')') and all(w in _TRANSFER_LEADING_WORDS for w in leading) and \
                re.match(r'^[\w.:-]+$', name):
            # `name()` or `name ()` defines a function; the body that follows runs only when it's called.
            self.argv, self.word, self.in_word = leading, '', False
            self._flush()  # a then, do or { before it still opens its compound
            self.items.append(('func', None, self.before, '', None))
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
        """A redirection's operator, an fd number written against it, and its target are not arguments."""
        text, word = self.text, self.word
        fd = int(word) if self.in_word and word.isdigit() and text[self.i] != '&' else None
        if fd is None:
            self._end_word()
        self.word, self.in_word = '', False
        op = re.match(r'&>>?|<<<|<<-|<<|<>|<&|>>|>&|>\||<|>', text[self.i:]).group(0)
        target, end = _transfer_read_word(text, self.i + len(op))
        if op in ('<<', '<<-'):
            self.heredocs.append((target, op == '<<-'))
        reads = op in ('<', '<<', '<<-', '<<<', '<>') and fd in (None, 0)
        writes = (op in ('>', '>>', '>|') and fd in (None, 1)) or op in ('&>', '&>>') or \
            (op == '>&' and fd in (None, 1) and not target.isdigit() and target != '-')
        if target != '/dev/null' or op in ('<<', '<<-', '<<<'):
            self.io |= ({'in'} if reads else set()) | ({'out'} if writes else set())
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
        if pair in ('&&', '||', '|&'):
            self._end_command('|' if pair == '|&' else pair)
            self.i += 1
        else:
            self._end_command(ch if ch in '&|' else ';')
        if ch == '\n':
            self._skip_heredocs()

    def _skip_heredocs(self) -> None:
        """A heredoc's body starts on the next line and is input, not commands."""
        text = self.text
        while self.heredocs:
            delimiter, strip_tabs = self.heredocs.pop(0)
            while self.i + 1 < len(text):
                end = text.find('\n', self.i + 1)
                line = text[self.i + 1:] if end < 0 else text[self.i + 1:end]
                self.i = len(text) - 1 if end < 0 else end
                if (line.lstrip('\t') if strip_tabs else line) == delimiter:
                    break

    def _end_word(self) -> None:
        if not self.in_word:
            return
        word, state = self.word, self.case[-1] if self.case else None
        self.word, self.in_word = '', False
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
        else:
            self.argv.append(word)

    def _esac(self) -> None:
        self._flush()
        self.case.pop()
        self.items.append(('esac', None, '', '', None))

    def _flush_leading(self) -> None:
        """Emit the reserved words that lead the current command, so a substitution in it runs only when they let it."""
        n = 0
        # `!` stays with its command, which still obeys the && or || before it.
        while n < len(self.argv) and self.argv[n] != '!' and \
                (self.argv[n] in _TRANSFER_LEADING_WORDS or self.argv[n] == 'function'):
            n += 2 if self.argv[n] == 'function' else 1
        if n:
            self.items.append(('cmd', self.argv[:n], self.before, '', set()))
            self.argv, self.before = self.argv[n:], ''

    def _flush(self) -> None:
        """Emit the reserved words read so far ahead of the compound command they lead."""
        if self.argv:
            self.items.append(('cmd', self.argv, self.before, '', self.io))
            self.argv, self.io, self.before = [], set(), ''

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
        self.argv, self.io = [], set()


def _transfer_words(text: str) -> List[str]:
    """A string's words, split as the shell splits them, without its operators."""
    return [w for item in (_TransferLexer(text).read() or []) if item[0] == 'cmd' for w in item[1]]


def _transfer_env(env: Dict, key: str) -> str:
    """A variable as the command sees it: set or unset in the command, else inherited from the hook's environment."""
    if key in env:
        return env[key]
    return '' if _TRANSFER_CLEAN_ENV in env else os.environ.get(key, '')


def _transfer_is_assignment(token: str) -> bool:
    return '=' in token and not token.startswith('-') and token.split('=', 1)[0].replace('_', '').isalnum()


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
        path = os.path.expanduser(value)
        cwd = path if os.path.isabs(path) else os.path.normpath(os.path.join(cwd or '', path))
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
    """Whether a command runs after its && or ||, given the last literal true (0) or false (1)."""
    return not ((before == '&&' and status == 1) or (before == '||' and status == 0))


def _transfer_runs(shell: Dict) -> Optional[bool]:
    return shell['frames'][-1]['runs'] if shell['frames'] else True


def _transfer_snapshot(shell: Dict) -> tuple:
    return shell['cwd'], dict(shell['exported'])


def _transfer_restore(shell: Dict, saved: Optional[tuple]) -> None:
    if saved:
        shell['cwd'], shell['exported'] = saved[0], dict(saved[1])


def _transfer_pop(frames: List[Dict], kinds: tuple) -> Optional[Dict]:
    """Close the innermost compound command of one of these kinds."""
    while frames:
        frame = frames.pop()
        if frame['kind'] in kinds:
            return frame
    return None


def _transfer_open(kind: str, runs: Optional[bool], shell: Dict) -> None:
    """Open a compound command; a function's body is only defined, so it never runs here."""
    if shell['function'] and kind != '$(':
        runs, shell['function'] = False, False
    shell['frames'].append({'kind': kind, 'base': runs, 'runs': runs, 'decided': False, 'saved': None,
                            'state': _transfer_snapshot(shell), 'status': shell['status'],
                            'calls': len(shell['calls']), 'piped_in': shell.pop('opening_pipe', False)})


def _transfer_close(frame: Dict, after: str, io: Optional[set], shell: Dict) -> None:
    """Give the commands inside a closed compound the pipes and redirections around it, as they share its stdin and
    stdout; a backgrounded compound keeps its cd and exports to itself."""
    calls = shell['calls']
    for i in range(frame['calls'], len(calls)):
        call = calls[i]
        calls[i] = call[:5] + (call[5] | (io or set()), call[6] or frame['piped_in'], call[7] or after == '|')
    if after == '&':
        shell['start'] = frame['state']


def _transfer_branch(keyword: str, frame: Dict, shell: Dict) -> None:
    """Start the body a then, elif, else or do opens: run, skipped or maybe run, by the literal status before it.
    A body that only maybe runs keeps its cd and exports to itself."""
    holds = {0: True, 1: False}.get(shell['status'])
    if keyword == 'then' and frame['kind'] == 'if':
        taken = _transfer_both(frame['runs'], holds)
        frame['decided'] = True if taken else None if taken is None and frame['decided'] is False else frame['decided']
    elif keyword in ('elif', 'else') and frame['kind'] == 'if':
        _transfer_restore(shell, frame['saved'])
        frame['saved'] = None
        taken = _transfer_both(frame['base'], {True: False, False: True}.get(frame['decided']))
    elif keyword == 'do' and frame['kind'] in _TRANSFER_LOOPS:
        holds = None if frame['kind'] in ('for', 'select') else holds
        taken = _transfer_both(frame['runs'], holds if holds is None or frame['kind'] == 'while' else not holds)
    else:
        return
    frame['runs'] = taken
    if taken is None and frame['saved'] is None:
        frame['saved'] = _transfer_snapshot(shell)


def _transfer_keyword(words: List[str], follows: bool, shell: Dict) -> List[str]:
    """Apply a command's leading reserved word to the open compound commands; the words after it."""
    keyword, rest, frames = words[0], words[1:], shell['frames']
    if keyword in ('{', 'if') + _TRANSFER_LOOPS:
        _transfer_open(keyword, _transfer_both(_transfer_runs(shell), follows), shell)
        return [] if keyword in ('for', 'select') else rest  # a for loop's header runs nothing
    if keyword == 'function':
        shell['function'] = True
        return rest[1:]
    if keyword == '!':
        return rest  # the caller negates the command it leads
    if keyword in ('}', 'fi', 'done'):
        frame = _transfer_pop(frames, {'}': ('{',), 'fi': ('if',), 'done': _TRANSFER_LOOPS}[keyword])
        _transfer_restore(shell, frame and frame['saved'])
        shell['closed'] = frame
        if keyword != '}':
            shell['status'] = None  # an if or loop that ran no body still succeeds
        return rest
    if frames:
        _transfer_branch(keyword, frames[-1], shell)
    return rest


def _transfer_marker(item: tuple, shell: Dict) -> None:
    """Apply a subshell, substitution, function or case marker to the open compound commands."""
    kind, _, before, after, io = item
    frames, follows = shell['frames'], _transfer_follows(before, shell['status'])
    if kind in ('(', '$(', 'case'):
        # A substitution's output is captured, so only a subshell or case shares the pipe around it.
        shell['opening_pipe'] = before == '|' and kind != '$('
        _transfer_open(kind, _transfer_both(_transfer_runs(shell), follows), shell)
        if kind == 'case':
            frames[-1]['runs'] = False  # nothing runs until an arm opens
    elif kind == 'func':
        shell['function'] = True
    elif kind == 'arm' and frames and frames[-1]['kind'] == 'case':
        _transfer_restore(shell, frames[-1]['saved'])
        frames[-1]['runs'] = _transfer_both(frames[-1]['base'], None)
        frames[-1]['saved'] = _transfer_snapshot(shell)
    elif kind in (')', 'esac'):
        frame = _transfer_pop(frames, ('(', '$(') if kind == ')' else ('case',))
        if frame:
            # A subshell's cd and exports end with it, as do a case arm's, which may not have run.
            _transfer_restore(shell, frame['state'] if kind == ')' else frame['saved'])
            if frame['kind'] == '$(' or frame['runs'] is False:
                shell['status'] = frame['status']
            elif kind == 'esac':
                shell['status'] = None
            if frame['kind'] != '$(':
                _transfer_close(frame, after, io, shell)


def _transfer_builtin(program: str, args: List[str], cwd: Optional[str], shell: Dict) -> None:
    """Apply a cd, export or unset to the commands after it."""
    if program == 'export':
        shell['exported'].update(a.split('=', 1) for a in args if _transfer_is_assignment(a))
    elif program == 'unset':
        # Unset hides the inherited value too, so it is kept as empty rather than dropped.
        shell['exported'].update((name, '') for name in args if not name.startswith('-'))
    else:
        target = os.path.expanduser(args[0]) if args else os.path.expanduser('~')
        shell['cwd'] = target if os.path.isabs(target) else os.path.normpath(os.path.join(cwd or '', target))


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


def _transfer_simple(words: List[str], before: str, after: str, io: set, shell: Dict, depth: int) -> List[tuple]:
    """The calls one simple command makes, after applying its cd, export or unset."""
    program, args, env, cwd, other_user = _transfer_program(words, shell['cwd'])
    env = env if _TRANSFER_CLEAN_ENV in env else {**shell['exported'], **env}
    shell['status'] = 0 if program in ('true', ':') else 1 if program == 'false' else None
    if program in ('cd', 'export', 'unset'):
        # A pipeline's commands run in subshells, so their cd and exports end with them.
        if before != '|' and after != '|':
            _transfer_builtin(program, args, cwd, shell)
        return []
    inner = _transfer_shell_string(program, args) if program else None
    if inner is not None:
        # The string's commands run as the wrapper's user and read its stdin and write its stdout.
        calls = _transfer_walk(_TransferLexer(inner, depth + 1).read() or [], cwd, env, depth + 1)
        return [call[:4] + (call[4] or other_user, call[5] | io, call[6] or before == '|', call[7] or after == '|')
                for call in calls]
    return [(program, args, env, cwd, other_user, io, before == '|', after == '|')] if program else []


def _transfer_command(item: tuple, shell: Dict, depth: int) -> List[tuple]:
    """The calls one simple command makes, if it runs or may run."""
    _, words, before, after, io = item
    follows = _transfer_follows(before, shell['status'])
    if before not in ('&&', '||', '|'):
        shell['start'] = _transfer_snapshot(shell)
    negated, shell['opening_pipe'] = False, before == '|'
    while words and words[0] in _TRANSFER_KEYWORDS:
        negation = words[0] == '!'
        negated = negated or negation
        words = _transfer_keyword(words, follows, shell)
        # A frame or branch keyword takes the && or || itself; `!` leaves it to the command it negates.
        follows = follows if negation else True
    shell.pop('opening_pipe', None)
    closed = shell.pop('closed', None)
    if closed:
        _transfer_close(closed, after, io, shell)
    calls = []
    if words and _transfer_both(_transfer_runs(shell), follows) is not False:
        calls = _transfer_simple(words, before, after, io, shell, depth)
        if negated:
            shell['status'] = {0: 1, 1: 0}.get(shell['status'])
    if after == '&':
        _transfer_restore(shell, shell['start'])  # a backgrounded list runs in a subshell
    return calls


def _transfer_walk(items: List[tuple], cwd: Optional[str], exported: Dict, depth: int) -> List[tuple]:
    """Each simple command that runs or may run, as (program, args, env, folder, as another user, redirections,
    piped in, piped out); commands a literal true or false skips, untaken branches and function bodies are left out."""
    calls = []
    shell = {'cwd': cwd, 'exported': dict(exported), 'status': None, 'frames': [], 'function': False,
             'start': (cwd, dict(exported)), 'calls': calls}
    for item in items:
        if item[0] == 'cmd':
            calls += _transfer_command(item, shell, depth)
        else:
            _transfer_marker(item, shell)
    return calls


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
        host = urlsplit(text if '://' in text else 'http://' + text).hostname or ''
        if host.isascii():
            return host
        import unicodedata
        # curl (libidn2) and the URL standard dial each non-ASCII label in punycode, keeping ß and a final sigma.
        return '.'.join(label if label.isascii() else 'xn--' + label.encode('punycode').decode('ascii')
                        for label in unicodedata.normalize('NFC', host).split('.'))
    except ValueError:
        return ''


def _transfer_curl(args: List[str]) -> List[tuple]:
    """curl's URLs: sent to when their request uploads a file, a form or a body, or uses a sending method; else fetched
    from. Each --next starts a request with options of its own."""
    endpoints, request = [], []
    for arg in args + ['--next']:
        if arg not in ('-:', '--next'):
            request.append(arg)
            continue
        urls, sends = _transfer_curl_request(request)
        endpoints += [('out' if sends else 'in', _transfer_url_host(url), None) for url in urls]
        request = []
    return endpoints


def _transfer_curl_request(args: List[str]) -> tuple:
    """(the URLs of one curl request, whether it sends data)."""
    urls, flags, method, i = [], set(), '', 0
    while i < len(args):
        arg, value, i = args[i], None, i + 1
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
    sends = flags & _TRANSFER_CURL_UPLOADS or method in _TRANSFER_SENDING_METHODS or \
        (flags & _TRANSFER_CURL_BODIES and not flags & {'-G', '--get'})
    return urls, bool(sends)


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


def _transfer_parse(program: str, args: List[str], env: Dict, other_user: bool, io: set, piped_in: bool,
                    piped_out: bool) -> Optional[tuple]:
    """(tool, [(direction, destination, ssh -G options or None)]) for a transfer command; None for any other."""
    tool, sends = _TRANSFER_TOOLS.get(program), 'in' in io or piped_in
    if tool in ('scp', 'sftp', 'rsync'):
        raw = _transfer_copy(tool, args, env)
    elif tool == 'ssh':
        raw = _transfer_ssh_pipe(args, sends, 'out' in io or piped_out)
    elif tool in ('curl', 'wget'):
        raw = _transfer_curl(args) if tool == 'curl' else _transfer_wget(args)
    elif tool in ('nc', 'socat'):
        raw = _transfer_netcat(args, sends) if tool == 'nc' else _transfer_socat(args, sends)
    elif tool in ('httpie', 'powershell'):
        raw = _transfer_httpie(args, sends) if tool == 'httpie' else _transfer_web_cmdlet(args, sends)
    else:
        return None
    # Under sudo, ssh runs as that user, with that user's config, which this can't read.
    return tool, [(d, dest, None if other_user else options) for d, dest, options in raw]


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
        if keyword.lower() == 'match' and any(w.lower().lstrip('!').startswith('exec') for w in words):
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
        kinds = [_transfer_kind(a) for a in addresses]
        state[key] = 'unresolved' if not kinds else 'public' if 'public' in kinds else kinds[0]
    return state[key]


def _transfer_endpoint(direction: str, destination: str, ssh_options: Optional[List[str]], cwd: Optional[str],
                       state: Dict) -> Optional[Dict]:
    """One place a transfer reached: its host as written without any login, the host ssh resolves it to, and the
    kind of address that host has."""
    destination = destination.strip()
    host = _transfer_url_host(destination) if '://' in destination else destination.rsplit('@', 1)[-1]
    host = _transfer_clean_host(host)
    if not host:
        return None
    resolved = (_transfer_ssh_hostname(destination, ssh_options, cwd, state) if ssh_options is not None else '') or host
    endpoint = {'direction': direction, 'host': host, 'resolved_host': resolved,
                'address': _transfer_address(resolved, state)}
    return {k: v for k, v in endpoint.items() if v}


def _transfer(command: Optional[str], cwd: Optional[str], state: Dict) -> Optional[Dict]:
    """Where a shell call's transfers went: the first transfer tool it ran and each place it sent to or fetched
    from; None when it ran no transfer. Never raises, and never takes longer than the turn's budget."""
    try:
        if not isinstance(command, str) or not command.strip():
            return None
        tool, endpoints = None, []
        for program, args, env, call_cwd, other_user, io, piped_in, piped_out in \
                _transfer_walk(_TransferLexer(command).read() or [], cwd, {}, 0):
            found = _transfer_parse(program, args, env, other_user, io, piped_in, piped_out)
            for direction, destination, ssh_options in (found[1] if found else []):
                if len(endpoints) >= _TRANSFER_MAX_ENDPOINTS:
                    break
                endpoint = _transfer_endpoint(direction, destination, ssh_options, call_cwd, state)
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