#!/usr/bin/env python3
"""
Real-time Cursor hook event processor with smart garbage collection.
Reads JSON events from stdin, appends to agent-audit.log, and processes them on stop events.
"""

import sys
import json
import os
import subprocess
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timedelta


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


def build_llm_exchange(events, api_key=None):
    """Build standard LLM exchange format from events."""
    messages = []
    assistant_tool_uses = []
    
    user_prompt = None
    assistant_response = None
    conversation_id = None
    model = None
    
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
            assistant_tool_uses.append({
                'type': hook_event_name,
                'command': event.get('command'),
                'output': event.get('output', '')
            })
        
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


def process_stop_event(conversation_id, generation_id=None, api_key=None):
    """Process stop event: convert to LLM format and send to API.

    Uses generation_id when available, falls back to conversation_id
    (Cursor's stop hook only provides conversation_id, not generation_id).
    """
    logs = load_existing_logs()

    if generation_id:
        # Prefer generation_id grouping when available
        grouped = group_events_by_generation(logs)
        for conv_id, generations in grouped.items():
            if generation_id in generations:
                events = generations[generation_id]
                exchange = build_llm_exchange(events, api_key)
                if exchange:
                    send_to_api(exchange, api_key)
                remaining_logs = [
                    log for log in logs
                    if log.get('event', {}).get('generation_id') != generation_id
                ]
                save_logs(remaining_logs)
                return

    # Fallback: use conversation_id to find all events for this conversation
    if conversation_id:
        events = [
            log for log in logs
            if log.get('event', {}).get('conversation_id') == conversation_id
        ]
        if events:
            exchange = build_llm_exchange(events, api_key)
            if exchange:
                send_to_api(exchange, api_key)
            remaining_logs = [
                log for log in logs
                if log.get('event', {}).get('conversation_id') != conversation_id
            ]
            save_logs(remaining_logs)


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
        
        # Process stop event (stop may not have generation_id, so use conversation_id)
        if hook_event_name == 'stop':
            process_stop_event(conversation_id, generation_id, api_key)
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