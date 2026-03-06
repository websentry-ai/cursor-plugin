# Unbound Cursor Extension — Implementation Plan

## Goal

Build a Cursor plugin that provides the same security, governance, and analytics capabilities as the existing Claude Code plugin (`claude-code-plugin`), targeting the Cursor hooks system. The core Unbound API logic is shared; only the platform adapter layer differs.

---

## Architecture Overview

```
cursor-extension/
├── .cursor-plugin/
│   ├── plugin.json              # Cursor plugin manifest
│   └── marketplace.json         # Cursor marketplace catalog
├── hooks/
│   └── hooks.json               # Cursor hook event configuration
├── scripts/
│   ├── hook-handler.py          # Central dispatcher (adapted for Cursor schemas)
│   ├── setup.py                 # Setup script (reused from claude-code-plugin, minor tweaks)
│   └── lib/
│       └── unbound.py           # Unbound API helpers (shared, with platform abstraction)
├── skills/
│   └── setup/
│       └── SKILL.md             # /unbound:setup onboarding skill (if Cursor supports skills)
├── enterprise/
│   ├── hooks.json.tmpl          # Enterprise MDM hooks template
│   └── README.md                # Enterprise deployment guide
├── tests/
│   ├── test_adapter.py          # Cursor schema adapter tests
│   ├── test_pretool.py          # PreToolUse hook tests (ported)
│   ├── test_prompt.py           # beforeSubmitPrompt tests (ported)
│   ├── test_session.py          # sessionEnd / postToolUse tests (ported)
│   ├── test_sanity.py           # Production readiness tests (ported)
│   └── requirements.txt         # pytest
├── README.md
├── LICENSE
└── .gitignore
```

---

## Schema Mapping Reference

This table drives all adapter work. Every input/output translation flows from these mappings.

### Hook Event Mapping

| Claude Code Hook | Cursor Hook | Notes |
|---|---|---|
| `PreToolUse` | `preToolUse` | camelCase in Cursor |
| `UserPromptSubmit` | `beforeSubmitPrompt` | Different name, different schema |
| `PostToolUse` | `postToolUse` | camelCase in Cursor |
| `Stop` | `sessionEnd` | Cursor's `sessionEnd` is richer (has `reason`, `duration_ms`) |

### Input Schema Differences

| Field | Claude Code | Cursor |
|---|---|---|
| Session ID | `session_id` | `conversation_id` |
| Tool name | `tool_name` | `tool_name` (same) |
| Tool input | `tool_input` | `tool_input` (same) |
| User prompt | `prompt` (in event) | `prompt` (in event) |
| Model | `model` | `model` (same) |
| Transcript | `transcript_path` | `transcript_path` (same) |
| Hook event name | Passed as CLI arg | `hook_event_name` field |

### Output Schema Differences

| Hook | Claude Code Output | Cursor Output |
|---|---|---|
| PreToolUse (allow) | `{"hookSpecificOutput": {"permissionDecision": "allow"}, "suppressOutput": true}` | `{"decision": "allow"}` |
| PreToolUse (deny) | `{"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "..."}, "suppressOutput": true}` | `{"decision": "deny", "reason": "..."}` |
| PreToolUse (ask) | `{"hookSpecificOutput": {"permissionDecision": "ask", ...}, "suppressOutput": true}` | `{"decision": "deny", "reason": "..."}` (Cursor has no "ask", map to deny) |
| UserPromptSubmit (block) | `{"decision": "block", "reason": "...", "suppressOutput": true}` | `{"continue": false, "user_message": "..."}` |
| UserPromptSubmit (allow) | *(no output)* | *(no output, or `{"continue": true}`)* |
| PostToolUse | *(no output, logs to audit)* | *(no output, logs to audit)* |
| Stop/sessionEnd | *(no output, sends exchange)* | *(no output, sends exchange)* |

### Exit Code Behavior

| Outcome | Claude Code | Cursor |
|---|---|---|
| Allow | Exit 0 + JSON | Exit 0 + JSON |
| Deny/Block | Exit 0 + JSON with deny | Exit 2 (or exit 0 + deny JSON) |
| Error (fail open) | Exit 0 + allow JSON | Exit 1 (non-0, non-2 = fail open) |

---

## Milestones

### Milestone 1: Project Scaffolding & Plugin Manifest
### Milestone 2: Cursor Schema Adapter Layer
### Milestone 3: Hook Handler (Cursor-Native)
### Milestone 4: Setup Flow (Skills + Setup Script)
### Milestone 5: Enterprise MDM Support
### Milestone 6: Tests
### Milestone 7: Documentation & Marketplace Submission

---

## Milestone 1: Project Scaffolding & Plugin Manifest

**Goal:** Set up the project structure, plugin manifest, and hooks config so Cursor recognizes it as a valid plugin.

- [ ] **1.1** Create `.cursor-plugin/plugin.json` manifest
  - Fields: `name`, `description`, `version`, `author`, `repository`, `license`, `keywords`
  - Set `"hooks": "hooks/hooks.json"` to register hooks
  - Set `"skills": "./skills/"` if Cursor supports skills (verify from docs)
  - Reference: `claude-code-plugin/.claude-plugin/plugin.json`

- [ ] **1.2** Create `.cursor-plugin/marketplace.json` catalog
  - Fields: `name`, `version`, `description`, `metadata`, `owner`, `plugins[]`
  - `source.repo` → `websentry-ai/cursor-extension`
  - `category` → `"security"`
  - Reference: `claude-code-plugin/.claude-plugin/marketplace.json`

- [ ] **1.3** Create `hooks/hooks.json` with Cursor hook events
  - Map all 4 hooks using Cursor's format:
    ```json
    {
      "hooks": {
        "preToolUse": [{"command": "python3 ${CURSOR_PLUGIN_ROOT}/scripts/hook-handler.py preToolUse", "timeout": 10, "matcher": "*"}],
        "postToolUse": [{"command": "python3 ${CURSOR_PLUGIN_ROOT}/scripts/hook-handler.py postToolUse", "timeout": 60}],
        "beforeSubmitPrompt": [{"command": "python3 ${CURSOR_PLUGIN_ROOT}/scripts/hook-handler.py beforeSubmitPrompt", "timeout": 60}],
        "sessionEnd": [{"command": "python3 ${CURSOR_PLUGIN_ROOT}/scripts/hook-handler.py sessionEnd", "timeout": 60}]
      }
    }
    ```
  - **Open question:** Does Cursor support `${CURSOR_PLUGIN_ROOT}` or similar variable in hook commands? If not, we need a relative path strategy. The docs show relative paths from project root (`.cursor/hooks/script.sh`). Need to verify how plugin-installed hooks resolve paths.

- [ ] **1.4** Create `.gitignore`
  - Copy from `claude-code-plugin/.gitignore`, add Cursor-specific entries (`.cursor/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`)

- [ ] **1.5** Create `LICENSE` (MIT)

---

## Milestone 2: Cursor Schema Adapter Layer

**Goal:** Build a thin adapter module that translates between Cursor's hook input/output schemas and the Unbound API's expected format. This is the core differentiator from the Claude Code plugin.

- [ ] **2.1** Create `scripts/lib/adapter.py` — Cursor-to-Unbound input normalizer
  - Function: `normalize_input(cursor_event: str, payload: dict) -> dict`
  - Maps Cursor field names to what `unbound.py` functions expect:
    - `conversation_id` → `session_id` (Cursor uses `conversation_id`)
    - `hook_event_name` injection from CLI arg
    - For `beforeSubmitPrompt`: extract `prompt` from payload
    - For `preToolUse`: payload already has `tool_name`, `tool_input` (same structure)
    - For `postToolUse`: payload has `tool_name`, `tool_input`, `tool_output`, `duration`
    - For `sessionEnd`: map `reason`, `duration_ms`, `session_id`

- [ ] **2.2** Add to `adapter.py` — Unbound-to-Cursor output formatter
  - Function: `format_pretool_output(unbound_response: dict) -> dict`
    - Unbound API returns `{"decision": "allow|deny|ask", "reason": "..."}`
    - Cursor expects `{"decision": "allow|deny", "reason": "..."}`
    - Map "ask" → "deny" (Cursor has no "ask" concept)
  - Function: `format_prompt_output(unbound_response: dict) -> dict`
    - Unbound API returns `{"decision": "deny", "reason": "..."}`
    - Cursor expects `{"continue": false, "user_message": "..."}`
    - Allow = `{"continue": true}` or empty
  - Function: `format_fail_open() -> dict`
    - PreToolUse: `{"decision": "allow"}`
    - BeforeSubmitPrompt: `{"continue": true}`

- [ ] **2.3** Add to `adapter.py` — Cursor tool name mapper
  - Cursor tool names: `Shell`, `Read`, `Write`, `Grep`, `Delete`, `MCP`, `Task`
  - Claude Code tool names: `Bash`, `Read`, `Write`, `Edit`, `Grep`, `Glob`, `WebFetch`, `WebSearch`, `Task`
  - Map Cursor → Unbound equivalents for `extract_command_for_pretool()`
  - `Shell` → treat like `Bash` (command field)
  - `Delete` → treat like file path extraction
  - `MCP` → use tool_name as command (pass through)

---

## Milestone 3: Hook Handler (Cursor-Native)

**Goal:** Create the central `hook-handler.py` dispatcher that uses the adapter layer + shared `unbound.py` library.

- [ ] **3.1** Copy `scripts/lib/unbound.py` from `claude-code-plugin`
  - Modify `AUDIT_LOG` path: `~/.claude/hooks/agent-audit.log` → `~/.cursor/hooks/agent-audit.log`
  - Modify `ERROR_LOG` path: `~/.claude/hooks/error.log` → `~/.cursor/hooks/error.log`
  - Keep `DEBUG_LOG` at `~/.unbound/logs/debug.jsonl` (shared across platforms)
  - Keep `OFFLINE_LOG` at `~/.unbound/logs/offline-events.jsonl` (shared)
  - Change `unbound_app_label` from `"claude-code"` to `"cursor"` in API payloads
  - **Future consideration:** Make these paths configurable or derive from a platform constant, to ease future multi-platform support in a shared repo.

- [ ] **3.2** Create `scripts/hook-handler.py` — Main dispatcher
  - Read event type from `sys.argv[1]` (same pattern as Claude Code)
  - Read JSON payload from stdin
  - Use `adapter.normalize_input()` to translate to Unbound format
  - Dispatch to handler functions
  - Handler map:
    ```python
    HANDLERS = {
        "preToolUse": handle_pre_tool_use,
        "beforeSubmitPrompt": handle_before_submit_prompt,
        "postToolUse": handle_post_tool_use,
        "sessionEnd": handle_session_end,
    }
    ```

- [ ] **3.3** Implement `handle_pre_tool_use(payload)`
  - Same logic as Claude Code's `handle_pre_tool_use()`
  - Call `_call_pretool_api(normalized_payload, api_key)`
  - Use `adapter.format_pretool_output()` to format response
  - Fail open: no API key → `{"decision": "allow"}`; API error → `{"decision": "allow"}`
  - Print JSON to stdout, exit 0

- [ ] **3.4** Implement `handle_before_submit_prompt(payload)`
  - Equivalent to Claude Code's `handle_user_prompt_submit()`
  - Call `_call_user_prompt_api(normalized_payload, api_key)`
  - If blocked: output `{"continue": false, "user_message": "<reason>"}`, exit 0
  - If allowed: log to audit, output nothing (or `{"continue": true}`), exit 0
  - Fail open on errors

- [ ] **3.5** Implement `handle_post_tool_use(payload)`
  - Append to audit log (same as Claude Code)
  - Cursor provides extra data: `tool_output`, `duration` — include in audit entry
  - No stdout output

- [ ] **3.6** Implement `handle_session_end(payload)`
  - Equivalent to Claude Code's `handle_stop()`
  - Cursor provides richer data: `reason` (completed/aborted/error/window_close/user_close), `duration_ms`, `final_status`, `error_message`
  - Include extra metadata in the exchange sent to Unbound API
  - Build exchange from audit log → send to API → clean up on success → offline fallback on failure

- [ ] **3.7** Implement debug logging
  - Same `write_debug_log()` pattern as Claude Code
  - Log to `~/.unbound/logs/debug.jsonl`

- [ ] **3.8** Implement offline fallback
  - Same `_write_offline()` pattern as Claude Code
  - Log to `~/.unbound/logs/offline-events.jsonl`

---

## Milestone 4: Setup Flow

**Goal:** Create the onboarding experience for users to configure their Unbound API key.

- [ ] **4.1** Copy and adapt `scripts/setup.py`
  - Change banner text from "Claude Code" to "Cursor"
  - Logic is identical: browser OAuth → local callback server → persist key to RC file
  - Same `--domain`, `--clear`, `--debug` flags

- [ ] **4.2** Create `skills/setup/SKILL.md` (if Cursor supports skills)
  - Adapt from Claude Code's `SKILL.md`
  - Replace `claude` CLI references with Cursor equivalents
  - Replace `/unbound:setup` invocation with whatever Cursor uses for skills
  - Update restart instructions: Cursor is a GUI app, so "restart Cursor" replaces "run `claude`"
  - **Open question:** Cursor docs mention "skills" as a plugin component. Need to verify the skill file format — is it `.mdc` (Cursor rules) or `.md` like Claude Code?

- [ ] **4.3** Verify skill invocation method in Cursor
  - Does Cursor support `/plugin:skill` syntax like Claude Code?
  - If not, provide alternative setup instructions (manual script run)

---

## Milestone 5: Enterprise MDM Support

**Goal:** Provide enterprise deployment templates for fleet-wide plugin enforcement.

- [ ] **5.1** Create `enterprise/hooks.json.tmpl`
  - Cursor enterprise hooks path:
    - macOS: `/Library/Application Support/Cursor/hooks.json`
    - Linux: `/etc/cursor/hooks.json`
    - Windows: `C:\ProgramData\Cursor\hooks.json`
  - This is the highest-priority config level (Enterprise > Team > Project > User)
  - Template should contain the same hook definitions as `hooks/hooks.json`

- [ ] **5.2** Create `enterprise/README.md`
  - Deployment steps for macOS, Linux, Windows
  - MDM API key provisioning (same Unbound MDM endpoint, `app_type=cursor`)
  - Shared fleet key option
  - Verification steps
  - Reference: `claude-code-plugin/enterprise/README.md`

- [ ] **5.3** Document Cursor's Team marketplace (enterprise alternative)
  - Cursor supports Team marketplaces where admins can mark plugins as "required"
  - Document this as an alternative to MDM for enterprise deployment
  - Admins import GitHub repo → set as required → auto-installs for all team members

---

## Milestone 6: Tests

**Goal:** Port and adapt the test suite from the Claude Code plugin, plus add Cursor-specific adapter tests.

- [ ] **6.1** Create `tests/test_adapter.py` — Adapter unit tests
  - Test `normalize_input()` for each hook event type
  - Test `format_pretool_output()` — allow, deny, ask→deny mapping
  - Test `format_prompt_output()` — block→continue:false, allow→continue:true
  - Test `format_fail_open()` for each hook type
  - Test Cursor tool name mapping (Shell→Bash, Delete→file_path, etc.)

- [ ] **6.2** Port `tests/test_pretool.py`
  - Adapt to use Cursor schemas (input/output)
  - Same coverage: stdin→API payload, API response→stdout, error paths
  - Verify `{"decision": "allow|deny"}` output format

- [ ] **6.3** Port `tests/test_prompt.py` (from `test_phase4_5.py` Phase 4 section)
  - Adapt to `beforeSubmitPrompt` schema
  - Verify `{"continue": false, "user_message": "..."}` for blocks
  - Verify no output for allows
  - Same error path coverage

- [ ] **6.4** Port `tests/test_session.py` (from `test_phase4_5.py` Phase 5 section)
  - `postToolUse` → audit log append (include Cursor's extra `duration` field)
  - `sessionEnd` → exchange build, API send, cleanup, offline fallback
  - Adapt session filtering tests

- [ ] **6.5** Port `tests/test_sanity.py`
  - `_make_log_entry()` structure tests
  - `write_debug_log()` file I/O tests
  - `_write_offline()` file I/O tests
  - `main()` dispatch routing tests (use Cursor event names)
  - Session filtering tests

- [ ] **6.6** Create `tests/requirements.txt`
  - `pytest`

---

## Milestone 7: Documentation & Marketplace Submission

**Goal:** Write user-facing docs and prepare for Cursor marketplace publication.

- [ ] **7.1** Create `README.md`
  - What it does (same 4 hooks, Cursor-native)
  - Installation from Cursor marketplace
  - Setup instructions (`/unbound:setup` or manual)
  - Verification steps
  - Configuration (UNBOUND_API_KEY)
  - Log file locations (Cursor-specific paths)
  - Project structure
  - Development (running tests)
  - License

- [ ] **7.2** Prepare marketplace submission
  - Cursor marketplace at cursor.com/marketplace/publish
  - Plugin must be open source (already MIT)
  - Manual security review by Cursor team
  - Ensure `.cursor-plugin/marketplace.json` is correct

- [ ] **7.3** End-to-end validation
  - Install plugin locally in Cursor
  - Run setup flow
  - Test PreToolUse block policy
  - Test DLP guardrail on prompt
  - Test audit logging appears on Unbound dashboard
  - Test session analytics on session close

---

## Open Questions

1. **Plugin root variable:** Does Cursor provide `${CURSOR_PLUGIN_ROOT}` or equivalent in hook commands? If not, how do plugin-installed hooks reference their own scripts? The docs show only relative paths.

2. **Skill file format:** Cursor mentions "skills" as a plugin component. Is the format `.md` (like Claude Code) or `.mdc` (Cursor rules format)? Need to verify.

3. **Matcher for preToolUse:** Claude Code uses `"matcher": "*"` to match all tools. Cursor matchers filter by tool type (`Shell`, `Read`, `Write`, etc.). Using no matcher may mean "match all" — need to verify.

4. **`conversation_id` vs `session_id`:** The Cursor docs show `conversation_id` in the common input schema. Need to confirm this maps to what the Unbound API expects as `conversation_id` (currently sent from `session_id` in Claude Code).

5. **Exit code strategy:** Cursor supports exit code 2 = block. Should we use exit codes for deny decisions instead of (or in addition to) JSON output? The docs say "Exit 0 – Hook succeeds; use JSON output" and "Exit 2 – Block the action". Using JSON output with exit 0 is probably more flexible (allows reason strings).

6. **Shared code repo:** This plan builds a standalone repo. If we later want to merge into a monorepo with `claude-code-plugin`, the main refactoring would be extracting `unbound.py` to a shared location and adjusting import paths.

---

## Dependencies

- Python 3.8+ (same as Claude Code plugin)
- No pip dependencies at runtime (uses stdlib + curl subprocess)
- `pytest` for testing only
- Cursor IDE with hooks support enabled

---

## Estimated Task Count

| Milestone | Tasks |
|---|---|
| 1. Scaffolding | 5 |
| 2. Adapter | 3 |
| 3. Hook Handler | 8 |
| 4. Setup Flow | 3 |
| 5. Enterprise | 3 |
| 6. Tests | 6 |
| 7. Docs & Publish | 3 |
| **Total** | **31** |
