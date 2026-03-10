#!/usr/bin/env python3
"""Unbound AI hook handler for Cursor.

Hooks:
  preToolUse        — calls Unbound API for command policy enforcement.
  beforeSubmitPrompt — calls Unbound API for guardrail checks (DLP/NSFW/Jailbreak).
  postToolUse       — audit logging for later exchange submission.
  sessionEnd        — builds full LLM exchange and sends to Unbound API.

Environment variables:
    UNBOUND_CURSOR_API_KEY  Bearer token for the Unbound API.
                     If unset, all hooks fail open (allow / no-op).
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Import shared Unbound API helpers and Cursor adapter
# ---------------------------------------------------------------------------
_LIB = Path(__file__).parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from unbound import (  # noqa: E402
    append_to_audit_log as _audit_log,
    build_llm_exchange as _build_exchange,
    cleanup_old_logs as _cleanup_logs,
    load_existing_logs as _load_logs,
    parse_transcript_file as _parse_transcript,
    process_pre_tool_use as _call_pretool_api,
    process_user_prompt_submit as _call_user_prompt_api,
    save_logs as _save_logs,
    send_to_api as _send_exchange,
)

from adapter import (  # noqa: E402
    format_fail_open,
    format_pretool_output,
    format_prompt_output,
    normalize_input,
)


LOG_DIR = Path.home() / ".unbound" / "logs"
DEBUG_LOG = LOG_DIR / "debug.jsonl"
OFFLINE_LOG = LOG_DIR / "offline-events.jsonl"
TRACE_LOG = LOG_DIR / "trace.log"


def _trace(msg: str) -> None:
    """Append a human-readable trace line to ~/.unbound/logs/trace.log."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        with TRACE_LOG.open("a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def write_debug_log(event: str, payload: dict) -> None:
    """Append a debug entry to ~/.unbound/logs/debug.jsonl."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "stdin": payload,
        }
        with DEBUG_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # Fail open: logging failure should never break hooks


def _write_offline(exchange: dict) -> None:
    """Write a failed exchange to ~/.unbound/logs/offline-events.jsonl."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "exchange": exchange,
        }
        with OFFLINE_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # Fail open: offline logging failure should never break hooks


def _make_log_entry(hook_event_name: str, payload: dict) -> dict:
    """Build a timestamped audit log entry, ensuring hook_event_name is present."""
    return {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
        "session_id": payload.get("session_id"),
        "event": {**payload, "hook_event_name": hook_event_name},
    }


# ---------------------------------------------------------------------------
# preToolUse
# ---------------------------------------------------------------------------

def handle_pre_tool_use(payload: dict) -> None:
    """preToolUse: call Unbound API for policy enforcement.

    Decision matrix:
      - No API key configured  -> allow (fail open)
      - API returns deny/ask   -> forward decision to Cursor
      - API error / timeout    -> allow (fail open)
      - Any unexpected error   -> allow (fail open)
    """
    api_key = os.getenv("UNBOUND_CURSOR_API_KEY")
    _trace(f"preToolUse | key={'set' if api_key else 'MISSING'} | tool={payload.get('tool_name','?')} | session={payload.get('session_id','?')[:8]}")
    if not api_key:
        _trace("preToolUse | SKIP: no API key, returning fail-open allow")
        print(json.dumps(format_fail_open("preToolUse")))
        return

    try:
        _trace("preToolUse | calling API...")
        api_response = _call_pretool_api(payload, api_key)
        _trace(f"preToolUse | API response: {json.dumps(api_response)[:200]}")
    except Exception as e:
        _trace(f"preToolUse | API EXCEPTION: {e}")
        api_response = {}

    result = format_pretool_output(api_response)
    _trace(f"preToolUse | output: {json.dumps(result)}")
    print(json.dumps(result))


# ---------------------------------------------------------------------------
# beforeSubmitPrompt
# ---------------------------------------------------------------------------

def handle_before_submit_prompt(payload: dict) -> None:
    """beforeSubmitPrompt: check prompt against Unbound guardrails.

    - If blocked (deny)  -> output {"continue": false, "user_message": "..."}.
                            The prompt is NOT logged (blocked prompts leave no trace).
    - If allowed         -> log to audit log so sessionEnd can include it in the exchange.
    - No API key         -> skip policy check, log, fail open.
    - API error/timeout  -> skip policy check, log, fail open.
    """
    api_key = os.getenv("UNBOUND_CURSOR_API_KEY")
    _trace(f"beforeSubmitPrompt | key={'set' if api_key else 'MISSING'} | prompt={payload.get('prompt','')[:50]} | session={payload.get('session_id','?')[:8]}")

    if api_key:
        try:
            _trace("beforeSubmitPrompt | calling API...")
            api_response = _call_user_prompt_api(payload, api_key)
            _trace(f"beforeSubmitPrompt | API response: {json.dumps(api_response)[:200]}")
        except Exception as e:
            _trace(f"beforeSubmitPrompt | API EXCEPTION: {e}")
            api_response = {}

        result = format_prompt_output(api_response)
        _trace(f"beforeSubmitPrompt | formatted: {json.dumps(result)}")
        if result.get("continue") is False:
            _trace("beforeSubmitPrompt | BLOCKED — not logging prompt")
            print(json.dumps(result))
            return  # Do not log blocked prompts
    else:
        _trace("beforeSubmitPrompt | SKIP: no API key, fail open")

    # Allowed — log so sessionEnd can reconstruct the full exchange
    _trace("beforeSubmitPrompt | allowed — logging to audit")
    _audit_log(_make_log_entry("beforeSubmitPrompt", payload))


# ---------------------------------------------------------------------------
# postToolUse
# ---------------------------------------------------------------------------

def handle_post_tool_use(payload: dict) -> None:
    """postToolUse: append tool use to audit log for aggregation on sessionEnd."""
    _trace(f"postToolUse | tool={payload.get('tool_name','?')} | session={payload.get('session_id','?')[:8]}")
    _audit_log(_make_log_entry("postToolUse", payload))


# ---------------------------------------------------------------------------
# sessionEnd
# ---------------------------------------------------------------------------

def handle_session_end(payload: dict) -> None:
    """sessionEnd: build the full LLM exchange and send to Unbound API.

    On success  -> clean up session entries from the audit log.
    On failure  -> write the exchange to ~/.unbound/logs/offline-events.jsonl.
    No API key  -> skip API call (audit log entries remain for manual inspection).
    """
    _audit_log(_make_log_entry("sessionEnd", payload))

    api_key = os.getenv("UNBOUND_CURSOR_API_KEY")
    if not api_key:
        return

    try:
        session_id = payload.get("session_id")
        transcript_path = payload.get("transcript_path")

        logs = _load_logs()
        session_events = []
        started = False
        user_prompt_ts = None

        for log in logs:
            sid = log.get("session_id") or log.get("event", {}).get("session_id")
            if sid != session_id:
                continue
            ev_name = (
                log.get("event", {}).get("hook_event_name")
                if "event" in log
                else log.get("hook_event_name")
            )
            if ev_name == "beforeSubmitPrompt":
                session_events = [log]
                started = True
                user_prompt_ts = log.get("timestamp")
            elif started:
                session_events.append(log)

        transcript_data = None
        if transcript_path and transcript_path != "undefined":
            transcript_data = _parse_transcript(transcript_path, user_prompt_ts)

        exchange = _build_exchange(session_events, transcript_data)

        if exchange:
            sent = _send_exchange(exchange, api_key)
            if sent:
                remaining = [
                    log for log in logs
                    if log.get("session_id") != session_id
                    and (
                        not log.get("event")
                        or log.get("event", {}).get("session_id") != session_id
                    )
                ]
                _save_logs(remaining)
            else:
                _write_offline(exchange)

        _cleanup_logs()

    except Exception:
        pass


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

HANDLERS = {
    "preToolUse": handle_pre_tool_use,
    "beforeSubmitPrompt": handle_before_submit_prompt,
    "postToolUse": handle_post_tool_use,
    "sessionEnd": handle_session_end,
}


def main() -> None:
    event = sys.argv[1] if len(sys.argv) > 1 else "Unknown"

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {"raw": raw}

    _trace(f"--- DISPATCH {event} ---")
    write_debug_log(event, payload)

    # Normalize Cursor payload for unbound.py compatibility
    payload = normalize_input(event, payload)
    _trace(f"normalized | session_id={payload.get('session_id','?')[:8]} | tool={payload.get('tool_name','')} | hook_event={payload.get('hook_event_name','')}")

    handler = HANDLERS.get(event)
    if handler:
        handler(payload)
    else:
        _trace(f"NO HANDLER for event: {event}")

    sys.exit(0)


if __name__ == "__main__":
    main()
