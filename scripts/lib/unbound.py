# Origin: https://github.com/websentry-ai/setup/blob/main/claude-code/hooks/unbound.py
# Adapted for Cursor: log paths changed to ~/.cursor/, app label changed to "cursor".
# When the upstream file changes, update this copy manually and bump the plugin version.

#!/usr/bin/env python3

import sys
import json
import os
import subprocess
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional


UNBOUND_GATEWAY_URL = "https://api.getunbound.ai"
AUDIT_LOG = Path.home() / ".cursor" / "hooks" / "agent-audit.log"
ERROR_LOG = Path.home() / ".cursor" / "hooks" / "error.log"
DEBUG_LOG = Path.home() / ".unbound" / "logs" / "debug.jsonl"


def _log_api_call(endpoint: str, success: bool, latency_ms: float, error: str = ""):
    """Log API call details (endpoint, success/failure, latency) to debug.jsonl."""
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "api_call",
            "api": endpoint,
            "success": success,
            "latency_ms": round(latency_ms, 1),
        }
        if error:
            entry["error"] = error
        with DEBUG_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def log_error(message: str):
    """Log error with timestamp to error.log, keeping only last 25 errors."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + 'Z'
    error_entry = f"{timestamp}: {message}\n"

    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
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
        pass


def load_existing_logs() -> List[Dict]:
    logs = []
    if AUDIT_LOG.exists():
        try:
            with open(AUDIT_LOG, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            logs.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except Exception:
            pass
    return logs


def save_logs(logs: List[Dict]):
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_LOG, 'w', encoding='utf-8') as f:
            for log in logs:
                f.write(json.dumps(log) + '\n')
    except Exception:
        pass


def append_to_audit_log(event_data: Dict):
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps(event_data) + '\n')
    except Exception:
        pass


def parse_transcript_file(transcript_path: str, user_prompt_timestamp: Optional[str] = None) -> Dict:
    conversation_data = {
        'user_messages': [],
        'assistant_messages': [],
        'tool_uses': []
    }

    if not transcript_path or not os.path.exists(transcript_path):
        return conversation_data

    try:
        with open(transcript_path, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue

                try:
                    entry = json.loads(line)
                    entry_type = entry.get('type', '')
                    entry_timestamp = entry.get('timestamp')

                    if entry_type == 'user':
                        message = entry.get('message', {})
                        if message.get('role') == 'user':
                            content = message.get('content', '')
                            if content:
                                conversation_data['user_messages'].append({
                                    'content': content,
                                    'timestamp': entry_timestamp
                                })

                    elif entry_type == 'assistant':
                        if user_prompt_timestamp and entry_timestamp:
                            if entry_timestamp <= user_prompt_timestamp:
                                continue

                        message = entry.get('message', {})
                        if message.get('role') == 'assistant':
                            content_array = message.get('content', [])
                            text_content = ''
                            for content_item in content_array:
                                if isinstance(content_item, dict) and content_item.get('type') == 'text':
                                    text_content = content_item.get('text', '')
                                    if text_content:
                                        conversation_data['assistant_messages'].append({
                                            'content': text_content,
                                            'timestamp': entry_timestamp
                                        })

                except json.JSONDecodeError:
                    continue

    except Exception:
        pass

    return conversation_data


def get_latest_user_prompt_for_session(session_id: str, transcript_path: Optional[str] = None) -> Optional[str]:
    """Get the most recent user prompt for this session."""
    logs = load_existing_logs()
    latest_prompt = None

    for log in logs:
        log_session = log.get('session_id') or log.get('event', {}).get('session_id')
        if log_session == session_id:
            event = log.get('event', {})
            # Check for both Claude Code and Cursor event names
            if event.get('hook_event_name') in ('UserPromptSubmit', 'beforeSubmitPrompt'):
                latest_prompt = event.get('prompt')

    if latest_prompt:
        return latest_prompt

    # Fallback: parse transcript file
    if transcript_path and transcript_path != 'undefined' and os.path.exists(transcript_path):
        data = parse_transcript_file(transcript_path)
        if data.get('user_messages'):
            return data['user_messages'][-1].get('content')

    return None


def extract_command_for_pretool(event: Dict) -> str:
    """Extract command from tool_input based on tool type.

    Note: Cursor tool names are mapped to Claude Code equivalents by the
    adapter before this function is called (e.g. Shell → Bash).
    """
    tool_input = event.get('tool_input', {})
    tool_name = event.get('tool_name', '')

    # Bash/Shell: command field
    if tool_name == 'Bash' and 'command' in tool_input:
        return tool_input['command']
    # File tools: file_path
    if tool_name in ['Write', 'Edit', 'Read', 'Delete'] and 'file_path' in tool_input:
        return tool_input['file_path']
    # Grep: pattern
    if tool_name == 'Grep' and 'pattern' in tool_input:
        return tool_input['pattern']
    # Glob: pattern
    if tool_name == 'Glob' and 'pattern' in tool_input:
        return tool_input['pattern']
    # WebFetch: url
    if tool_name == 'WebFetch' and 'url' in tool_input:
        return tool_input['url']
    # WebSearch: query
    if tool_name == 'WebSearch' and 'query' in tool_input:
        return tool_input['query']
    # Task: prompt
    if tool_name == 'Task' and 'prompt' in tool_input:
        return tool_input['prompt']
    # MCP: tool_name from input or fall back
    if tool_name == 'MCP':
        return tool_input.get('tool_name', tool_name)
    # Default: tool name
    return tool_name


def send_to_hook_api(request_body: Dict, api_key: str) -> Dict:
    """Send request to /v1/hooks/pretool endpoint."""
    endpoint = "/v1/hooks/pretool"
    if not api_key:
        return {}

    t0 = time.monotonic()
    try:
        url = f"{UNBOUND_GATEWAY_URL}{endpoint}"
        data = json.dumps(request_body)

        result = subprocess.run(
            ["curl", "-fsSL", "-X", "POST",
             "--connect-timeout", "3", "--max-time", "10",
             "-H", f"Authorization: Bearer {api_key}",
             "-H", "Content-Type: application/json",
             "-d", data, url],
            capture_output=True,
            timeout=12
        )

        latency = (time.monotonic() - t0) * 1000
        if result.returncode == 0 and result.stdout:
            parsed = json.loads(result.stdout.decode('utf-8'))
            _log_api_call(endpoint, True, latency)
            return parsed
        stderr = result.stderr.decode('utf-8', errors='ignore').strip() if result.stderr else ""
        _log_api_call(endpoint, False, latency, error=stderr or f"curl exit {result.returncode}")
        return {}
    except Exception as e:
        latency = (time.monotonic() - t0) * 1000
        log_error(f"Hook API error: {str(e)}")
        _log_api_call(endpoint, False, latency, error=str(e))
        return {}


def process_pre_tool_use(event: Dict, api_key: str) -> Dict:
    """Process preToolUse event — returns raw API response (no platform formatting).

    The caller (hook-handler.py) is responsible for formatting the response
    using adapter.format_pretool_output().
    """
    session_id = event.get('session_id')
    model = event.get('model') or 'auto'
    transcript_path = event.get('transcript_path')
    tool_name = event.get('tool_name', '')

    user_prompt = get_latest_user_prompt_for_session(session_id, transcript_path)
    command = extract_command_for_pretool(event)

    request_body = {
        'conversation_id': session_id,
        'unbound_app_label': 'cursor',
        'model': model,
        'event_name': 'tool_use',
        'pre_tool_use_data': {
            'command': command,
            'tool_name': tool_name,
            'metadata': event
        },
        'messages': [{'role': 'user', 'content': user_prompt}] if user_prompt else []
    }

    return send_to_hook_api(request_body, api_key)


def process_user_prompt_submit(event: Dict, api_key: str) -> Dict:
    """Process beforeSubmitPrompt event — returns raw API response.

    The caller (hook-handler.py) is responsible for formatting the response
    using adapter.format_prompt_output().
    """
    session_id = event.get('session_id')
    model = event.get('model') or 'auto'
    prompt = event.get('prompt', '')

    request_body = {
        'conversation_id': session_id,
        'unbound_app_label': 'cursor',
        'model': model,
        'event_name': 'user_prompt',
        'messages': [{'role': 'user', 'content': prompt}] if prompt else []
    }

    return send_to_hook_api(request_body, api_key)


def build_llm_exchange(events: List[Dict], main_transcript_data: Optional[Dict] = None) -> Optional[Dict]:
    messages = []
    assistant_tool_uses = []
    all_assistant_responses = []

    user_prompt = None
    user_prompt_timestamp = None
    session_id = None
    permission_mode = None

    # Accept both Claude Code and Cursor event names
    prompt_event_names = ('UserPromptSubmit', 'beforeSubmitPrompt')
    tool_event_names = ('PostToolUse', 'postToolUse')

    for log_entry in events:
        event = log_entry.get('event', {}) if 'event' in log_entry else log_entry
        if event.get('hook_event_name') in prompt_event_names:
            user_prompt = event.get('prompt')
            user_prompt_timestamp = log_entry.get('timestamp')
            break

    if main_transcript_data and user_prompt_timestamp:
        for assistant_msg in main_transcript_data.get('assistant_messages', []):
            msg_timestamp = assistant_msg.get('timestamp')
            content = assistant_msg.get('content', '')

            if msg_timestamp and msg_timestamp > user_prompt_timestamp:
                if content:
                    all_assistant_responses.append(content)

    for log_entry in events:
        event = log_entry.get('event', {}) if 'event' in log_entry else log_entry
        hook_event_name = event.get('hook_event_name')

        if not session_id:
            session_id = event.get('session_id')

        if not permission_mode:
            permission_mode = event.get('permission_mode')

        if hook_event_name in prompt_event_names:
            prompt = event.get('prompt')
            if prompt:
                user_prompt = prompt

        elif hook_event_name in tool_event_names:
            tool_name = event.get('tool_name')
            tool_input = event.get('tool_input', {})
            tool_response = event.get('tool_response', {})

            if 'content' in tool_response and 'content' in tool_input:
                if tool_response['content'] == tool_input['content']:
                    tool_response = {k: v for k, v in tool_response.items() if k != 'content'}

            assistant_tool_uses.append({
                'type': 'PostToolUse',
                'tool_name': tool_name,
                'tool_input': tool_input,
                'tool_response': tool_response
            })

    assistant_response = '\n\n'.join(all_assistant_responses) if all_assistant_responses else ""

    if user_prompt:
        messages.append({'role': 'user', 'content': user_prompt})

    if assistant_response or assistant_tool_uses:
        assistant_msg = {
            'role': 'assistant',
            'content': assistant_response
        }

        if assistant_tool_uses:
            assistant_msg['tool_use'] = assistant_tool_uses

        messages.append(assistant_msg)

    if len(messages) == 1 and messages[0]['role'] == 'user':
        return None

    if not messages:
        return None

    if not permission_mode:
        permission_mode = 'default'

    exchange = {
        'conversation_id': session_id or 'unknown',
        'model': 'auto',
        'messages': messages,
        'permission_mode': permission_mode
    }

    return exchange


def send_to_api(exchange: Dict, api_key: str) -> bool:
    """Send exchange data to Unbound API."""
    endpoint = "/v1/hooks/claude"
    if not api_key:
        log_error("No API key present in send_to_api function")
        return False

    t0 = time.monotonic()
    try:
        url = f"{UNBOUND_GATEWAY_URL}{endpoint}"
        data = json.dumps(exchange)

        result = subprocess.run(
            ["curl", "-fsSL", "-X", "POST",
             "--connect-timeout", "5", "--max-time", "10",
             "-H", f"Authorization: Bearer {api_key}",
             "-H", "Content-Type: application/json", "-d", data, url],
            capture_output=True,
            timeout=15
        )

        latency = (time.monotonic() - t0) * 1000
        if result.returncode != 0:
            error_msg = result.stderr.decode('utf-8', errors='ignore').strip() if result.stderr else "Unknown error"
            log_error(f"API request failed: {error_msg}")
            _log_api_call(endpoint, False, latency, error=error_msg)
            return False
        _log_api_call(endpoint, True, latency)
        return True
    except Exception as e:
        latency = (time.monotonic() - t0) * 1000
        log_error(f"Exception in send_to_api: {str(e)}")
        _log_api_call(endpoint, False, latency, error=str(e))
        return False


def cleanup_old_logs():
    logs = load_existing_logs()

    if len(logs) <= 50:
        return

    session_order = []
    seen_sessions = set()

    for log in logs:
        session_id = log.get('session_id')
        if session_id and session_id not in seen_sessions:
            session_order.append(session_id)
            seen_sessions.add(session_id)

    if len(session_order) > 1:
        most_recent_session = session_order[-1]
        kept_logs = [
            log for log in logs
            if log.get('session_id') == most_recent_session
        ]
        save_logs(kept_logs)
