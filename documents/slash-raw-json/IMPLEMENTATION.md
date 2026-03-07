# Implementation Plan: 1722252456479425985 - [Bug] Slash command returns raw JSON to Telegram chat

## Overview

Slash commands like `/remember` and `/plan` sent via Telegram result in raw JSON payloads (`{"status": "completed", "session_id": "..."}`) being forwarded to the user. The fix addresses three layers: (1) the fallback in `_handle_server_command` that dumps raw JSON for unmatched command results, (2) a missing catch-all handler for unregistered Telegram slash commands that should be routed as prompts to the gateway, and (3) a defensive JSON-detection filter in the response pipeline to prevent structured data from leaking to the user.

## Technical Approach

The Telegram bot currently has explicit `CommandHandler` registrations for server commands (`/sessions`, `/new`, `/clear`, etc.) and `/skill`. Unregistered commands like `/remember` or `/plan` are not caught by any handler because `handle_text` uses `filters.TEXT & ~filters.COMMAND`, which excludes messages starting with `/`. The fix adds a catch-all `MessageHandler(filters.COMMAND, ...)` placed last in the handler chain to intercept any unhandled slash commands and route them as prompts to the gateway (the same path as `/skill`). Additionally, the `_handle_server_command` fallback is hardened to never return raw JSON, and the response pipeline gains a JSON-detection helper that converts any accidental JSON payloads into human-readable messages.

The Discord bot's `_handle_server_command` has an identical fallback on line 309 and will receive the same fix for consistency.

## Reference Patterns

| Pattern | Source File | Usage |
|---------|------------|-------|
| `cmd_skill` streaming handler | `/usr/src/app/clients/telegram_bot.py:358-374` | Pattern for routing unregistered commands as prompts to gateway |
| `_handle_server_command` formatters | `/usr/src/app/clients/telegram_bot.py:224-262` | Pattern for command response formatting (and the fallback bug) |
| `_strip_markdown` | `/usr/src/app/clients/telegram_bot.py:96-113` | Text cleanup before sending to Telegram |
| `handle_text` | `/usr/src/app/clients/telegram_bot.py:380-412` | Pattern for streaming message with lock and error handling |
| Unit test structure | `/usr/src/app/tests/unit/test_audit.py` | Test class pattern with pytest, MagicMock |

## Models & Schemas

No new models or schemas needed. This is a pure behavior fix in the Telegram bot client layer.

## Implementation Steps

### Step 1: Add JSON detection helper to Telegram bot

Add a helper function `_looks_like_json(text)` that detects when a response looks like a raw JSON payload. This function is used downstream to prevent JSON from leaking to the user.

**Files:**
- Modify: `/usr/src/app/clients/telegram_bot.py` -- add `_looks_like_json()` helper function and `_sanitize_response()` function

**Test First (unit):** `tests/unit/test_telegram_response_sanitizer.py`
- [ ] `test_looks_like_json_detects_json_object` -- asserts `_looks_like_json('{"status": "completed"}')` returns `True`
- [ ] `test_looks_like_json_detects_json_array` -- asserts `_looks_like_json('[{"id": "abc"}]')` returns `True`
- [ ] `test_looks_like_json_rejects_plain_text` -- asserts `_looks_like_json('Hello world')` returns `False`
- [ ] `test_looks_like_json_rejects_text_with_braces` -- asserts `_looks_like_json('Use {name} as a placeholder')` returns `False` (brace in text but not valid JSON)
- [ ] `test_looks_like_json_handles_empty_string` -- asserts `_looks_like_json('')` returns `False`
- [ ] `test_looks_like_json_handles_whitespace_wrapped_json` -- asserts `_looks_like_json('  {"key": "val"}  ')` returns `True`
- [ ] `test_sanitize_response_passes_plain_text` -- asserts `_sanitize_response('Hello there')` returns `'Hello there'`
- [ ] `test_sanitize_response_replaces_json_with_confirmation` -- asserts `_sanitize_response('{"status": "completed", "session_id": "abc"}')` returns a human-readable string (not containing `{`)
- [ ] `test_sanitize_response_preserves_no_response` -- asserts `_sanitize_response('(No response)')` returns `'(No response)'`

**Then Implement:**
- [ ] Add `_looks_like_json(text: str) -> bool` function after the `_strip_markdown` function in `telegram_bot.py`. It should `text.strip()`, then check if the result starts with `{` or `[`, and attempt `json.loads()` -- returning `True` only on valid JSON parse. This avoids false positives on text that incidentally contains braces.
- [ ] Add `_sanitize_response(text: str) -> str` function that returns the text unchanged if `_looks_like_json` returns `False`, and returns `"Done."` if the text is detected as raw JSON.

**Verify:** `pytest tests/unit/test_telegram_response_sanitizer.py -v` -- all tests pass.

---

### Step 2: Harden `_handle_server_command` fallback in Telegram bot

The fallback `return json.dumps(result, indent=2)` at line 262 of `telegram_bot.py` is the direct cause of raw JSON leaking for unmatched command results. Replace it with a human-readable message.

**Files:**
- Modify: `/usr/src/app/clients/telegram_bot.py` -- change the fallback in `_handle_server_command` from `json.dumps(result, indent=2)` to a human-readable string

**Test First (unit):** `tests/unit/test_telegram_server_commands.py`
- [ ] `test_handle_server_command_fallback_no_json` -- mock `gateway.server_command` to return an unrecognized dict `{"foo": "bar"}`, call `_handle_server_command` with an unmatched command, assert the return value does NOT contain `{` or `}`
- [ ] `test_handle_server_command_error_formats_message` -- mock `gateway.server_command` to return `{"error": "something"}`, assert the return starts with `"Error:"`
- [ ] `test_handle_server_command_sessions_formats_list` -- mock `gateway.server_command` to return a session list, assert the return contains "Sessions" or session-like text, not raw JSON
- [ ] `test_handle_server_command_new_formats_short_id` -- mock `gateway.server_command` to return `{"id": "abcd1234-full-uuid", "status": "running"}`, assert return contains first 8 chars of ID

**Then Implement:**
- [ ] Replace line 262 in `telegram_bot.py` (`return json.dumps(result, indent=2)`) with `return "Done."` -- a safe human-readable fallback for any unmatched server command response.

**Verify:** `pytest tests/unit/test_telegram_server_commands.py -v` -- all tests pass.

---

### Step 3: Harden `_handle_server_command` fallback in Discord bot

Apply the same fix to the Discord bot for consistency. The Discord bot has the identical `json.dumps(result, indent=2)` fallback at line 309 of `discord_bot.py`.

**Files:**
- Modify: `/usr/src/app/clients/discord_bot.py` -- change the fallback in `_handle_server_command` from `json.dumps(result, indent=2)` to a human-readable string

**Test First (unit):** `tests/unit/test_discord_server_commands.py`
- [ ] `test_discord_handle_server_command_fallback_no_json` -- mock `gateway.server_command` to return an unrecognized dict, assert the return value does NOT contain raw JSON

**Then Implement:**
- [ ] Replace line 309 in `discord_bot.py` (`return json.dumps(result, indent=2)`) with `return "Done."` -- same safe fallback as the Telegram bot.

**Verify:** `pytest tests/unit/test_discord_server_commands.py -v` -- all tests pass.

---

### Step 4: Add catch-all handler for unregistered slash commands in Telegram bot

Add a `MessageHandler(filters.COMMAND, handle_unknown_command)` at the END of the handler chain. This catches any `/something` that wasn't handled by a registered `CommandHandler`. The handler routes the command text as a regular prompt to the gateway (same streaming path as `handle_text`), allowing Claude to process skills like `/remember` natively.

**Files:**
- Modify: `/usr/src/app/clients/telegram_bot.py` -- add `handle_unknown_command` function and register it as the last handler

**Test First (unit):** `tests/unit/test_telegram_unknown_commands.py`
- [ ] `test_unknown_command_routes_to_stream` -- mock `gateway.query_stream` to yield `["Memory stored."]`, simulate `/remember buy milk`, assert the reply text is `"Memory stored."` (not JSON)
- [ ] `test_unknown_command_strips_bot_mention` -- in group chat, simulate `/remember@botname something`, assert the prompt sent to gateway is `/remember something` (bot username stripped)
- [ ] `test_unknown_command_auth_check_blocks_unauthorized` -- simulate an unauthorized user sending `/remember`, assert `reply_text` is called with `"Not authorized."`
- [ ] `test_unknown_command_applies_sanitize` -- mock `gateway.query_stream` to yield `['{"status": "completed"}']`, assert the final message sent to user does NOT contain raw JSON

**Then Implement:**
- [ ] Add `handle_unknown_command(update, context)` function after `handle_text` in `telegram_bot.py`. Follow the same pattern as `handle_text`: auth check, lock acquisition, send placeholder `"\u2026"`, call `_stream_to_message`, send extra chunks. The key difference: the content IS the command text (including the `/` prefix), which gets sent as a prompt to the gateway so Claude processes it as a skill invocation.
- [ ] In `_stream_to_message`, after the final `_strip_markdown` call, apply `_sanitize_response` to each chunk in `final_chunks` before returning, to prevent JSON from leaking through any code path.
- [ ] Register the handler at the bottom of the handler chain (after `handle_voice`): `app.add_handler(MessageHandler(filters.COMMAND, handle_unknown_command))`. This ensures registered `CommandHandler` entries are matched first, and only truly unhandled commands reach this catch-all.

**Verify:** `pytest tests/unit/test_telegram_unknown_commands.py -v` -- all tests pass.

---

### Step 5: Apply sanitization to existing streaming responses

Apply `_sanitize_response` inside `_stream_to_message` to ensure that even existing code paths (like `cmd_skill` and `handle_text`) never forward raw JSON to the user. This is the defensive last-line filter.

**Files:**
- Modify: `/usr/src/app/clients/telegram_bot.py` -- modify `_stream_to_message` to sanitize final output

**Test First (unit):** `tests/unit/test_telegram_stream_sanitization.py`
- [ ] `test_stream_to_message_sanitizes_json_response` -- mock `gateway.query_stream` to yield only `['{"status": "completed", "session_id": "abc"}']`, verify that the text shown to the user (via `sent.edit_text`) does NOT contain `{` or `}`
- [ ] `test_stream_to_message_preserves_normal_text` -- mock `gateway.query_stream` to yield `["Hello, here is your answer."]`, verify that the text shown to the user is `"Hello, here is your answer."`
- [ ] `test_stream_to_message_sanitizes_only_final_not_preview` -- mock `gateway.query_stream` to yield JSON in a single chunk, verify the preview during streaming is allowed to contain anything (only the final result is sanitized)

**Then Implement:**
- [ ] In `_stream_to_message`, after computing `final_chunks = _chunk_message(_strip_markdown(accumulated))`, apply `_sanitize_response` to each chunk: `final_chunks = [_sanitize_response(c) for c in final_chunks]`. This ensures the final text sent to the user is never raw JSON, regardless of how the response was generated.

**Verify:** `pytest tests/unit/test_telegram_stream_sanitization.py -v` -- all tests pass.

---

### Step 6: Full integration test across all slash command paths

End-to-end tests verifying that no code path in the Telegram bot leaks raw JSON to the user.

**Files:**
- Create: `tests/unit/test_telegram_no_json_leak.py` -- comprehensive tests covering all AC

**Test First (unit):** `tests/unit/test_telegram_no_json_leak.py`
- [ ] `test_server_command_new_returns_readable` -- simulate `/new` command, mock gateway, assert response is human-readable (contains session ID fragment, no JSON braces)
- [ ] `test_server_command_clear_returns_readable` -- simulate `/clear` command, mock gateway, assert response is human-readable
- [ ] `test_skill_command_json_result_sanitized` -- simulate `/skill remember`, mock `query_stream` to yield JSON, assert final message is `"Done."` not JSON
- [ ] `test_unknown_slash_command_json_result_sanitized` -- simulate `/remember buy milk`, assert no JSON in response
- [ ] `test_regular_text_json_result_sanitized` -- simulate plain text message where Claude responds with JSON, assert it's sanitized
- [ ] `test_session_completion_payload_never_leaks` -- mock `query_stream` to yield `'{"status": "completed", "session_id": "abc-123"}'` (the exact payload from the bug report), assert it is replaced with `"Done."`

**Then Implement:**
- No new implementation -- these tests validate the changes from Steps 1-5.

**Verify:** `pytest tests/unit/test_telegram_no_json_leak.py -v` -- all tests pass.

---

## Integration Checklist

- [ ] No new routes needed in `server.py` -- fix is entirely client-side
- [ ] No new MCP tools needed
- [ ] No config additions needed
- [ ] No new dependencies needed
- [x] Handler registration order matters: `MessageHandler(filters.COMMAND, handle_unknown_command)` MUST be added LAST, after all `CommandHandler` entries, so it only catches truly unmatched commands

## Build Verification

- [ ] `pytest tests/unit/test_telegram_response_sanitizer.py -v` passes
- [ ] `pytest tests/unit/test_telegram_server_commands.py -v` passes
- [ ] `pytest tests/unit/test_discord_server_commands.py -v` passes
- [ ] `pytest tests/unit/test_telegram_unknown_commands.py -v` passes
- [ ] `pytest tests/unit/test_telegram_stream_sanitization.py -v` passes
- [ ] `pytest tests/unit/test_telegram_no_json_leak.py -v` passes
- [ ] `pytest -v` passes (full suite)
- [ ] `ruff check .` passes
- [ ] All ACs addressed:
  - AC1: Slash commands no longer return raw JSON (catch-all handler + sanitization)
  - AC2: Commands complete with brief confirmation ("Done.") or normal Claude response
  - AC3: No structured payloads leak (JSON detection + sanitization in `_stream_to_message`)
  - AC4: Session completion tracked internally (no change to server.py -- completion payloads intercepted at client layer)
