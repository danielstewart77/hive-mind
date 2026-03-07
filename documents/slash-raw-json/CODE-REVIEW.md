# Code Review: 1722252456479425985 - [Bug] Slash command returns raw JSON to Telegram chat

## Summary

Clean, well-structured bug fix that addresses the root cause (raw `json.dumps` fallback in `_handle_server_command`) and adds two layers of defense: a catch-all handler for unregistered slash commands, and a JSON-detection sanitizer in the streaming pipeline. The implementation follows the plan faithfully, tests are thorough (35 tests, all passing), and the code is consistent with existing patterns.

**Verdict:** APPROVED

## Acceptance Criteria Coverage

| # | Criterion | Status | Covered By |
|---|-----------|--------|------------|
| 1 | Slash commands (`/remember`, `/plan`, etc.) no longer return raw JSON to the chat | Implemented + Tested | `handle_unknown_command` catch-all in `telegram_bot.py:593-629`, `_handle_server_command` fallback changed to `"Done."` at line 287, tested in `test_telegram_unknown_commands.py`, `test_telegram_no_json_leak.py` |
| 2 | Commands either complete silently or send a brief, human-readable confirmation message | Implemented + Tested | `_sanitize_response` returns `"Done."` for JSON payloads (line 134-138), `_handle_server_command` fallback returns `"Done."` (line 287), tested in `test_telegram_response_sanitizer.py`, `test_telegram_server_commands.py` |
| 3 | No structured response payloads leak to the user-facing Telegram interface | Implemented + Tested | `_sanitize_response` applied in `_stream_to_message` (line 193), tested in `test_telegram_stream_sanitization.py`, `test_telegram_no_json_leak.py` |
| 4 | Session completion is internally tracked without exposing implementation details | Implemented + Tested | No changes to `server.py` -- completion payloads are intercepted at the client layer. Tested via `test_session_completion_payload_never_leaks` |

## Files Reviewed

| File | Status | Findings |
|------|--------|----------|
| `/usr/src/app/clients/telegram_bot.py` | OK | Three changes: JSON helpers added, `_handle_server_command` fallback fixed, `handle_unknown_command` catch-all added, sanitization in `_stream_to_message`. All correct. |
| `/usr/src/app/clients/discord_bot.py` | OK | Fallback changed from `json.dumps(result, indent=2)` to `"Done."`. `import json` removed (safe -- no remaining usage of the module). |
| `/usr/src/app/tests/unit/test_telegram_response_sanitizer.py` | OK | 13 tests covering `_looks_like_json` and `_sanitize_response` edge cases. |
| `/usr/src/app/tests/unit/test_telegram_server_commands.py` | OK | 4 tests covering `_handle_server_command` fallback, error formatting, session list, and `/new` formatting. |
| `/usr/src/app/tests/unit/test_discord_server_commands.py` | OK | 3 tests covering Discord bot's `_handle_server_command` fallback, error, and `/new`. |
| `/usr/src/app/tests/unit/test_telegram_unknown_commands.py` | OK | 5 tests covering routing, bot mention stripping, auth blocking, sanitization, and lock waiting. |
| `/usr/src/app/tests/unit/test_telegram_stream_sanitization.py` | OK | 4 tests covering JSON sanitization in `_stream_to_message`, normal text preservation, preview vs final distinction, and `(No response)` passthrough. |
| `/usr/src/app/tests/unit/test_telegram_no_json_leak.py` | OK | 6 integration-level tests covering `/new`, `/clear`, `/skill`, unknown commands, regular text, and the exact bug-report payload. |
| `/usr/src/app/tests/unit/conftest.py` | OK (pre-existing) | Mocks third-party modules. Not modified by this story. |

## Findings

### Critical

> None.

### Major

> None.

### Minor

#### M1: Discord bot streaming path lacks JSON sanitization

- **File:** `/usr/src/app/clients/discord_bot.py:254`
- **Dimension:** Consistency
- **Description:** The Telegram bot applies `_sanitize_response` in `_stream_to_message` (line 193) as a defensive last-line filter. The Discord bot's `_stream_to_message` (line 254) does not have this defensive layer. While the implementation plan only specified fixing the `_handle_server_command` fallback for Discord (Step 3), the same class of bug could occur in Discord if the gateway ever returns JSON through the streaming path. This is a defense-in-depth gap, not a current bug.
- **Suggested Fix:** Add `_looks_like_json` and `_sanitize_response` helpers to `discord_bot.py` and apply sanitization in its `_stream_to_message`. This could be deferred to a follow-up card.

#### M2: `handle_unknown_command` does not check group chat @mention

- **File:** `/usr/src/app/clients/telegram_bot.py:593-629`
- **Dimension:** Consistency
- **Description:** The `handle_text` handler (line 410-412) checks that the bot is @mentioned in group chats before responding. The `handle_unknown_command` catch-all does not perform this check -- it will respond to any `/unregistered_command` in group chats even without an @mention. The `filters.COMMAND` filter already matches all commands in groups. This could cause the bot to respond to commands not directed at it. This is low risk since most Telegram groups use BotFather privacy mode which only delivers commands to bots, but it is an inconsistency with `handle_text`.
- **Suggested Fix:** Add the same group-chat @mention check from `handle_text` to `handle_unknown_command`, or note that Telegram's privacy mode makes this unnecessary for commands (commands are always delivered to bots in groups regardless of @mention). Consider documenting the intentional difference if it is by design.

### Nits

#### N1: Unused `asyncio` import removal is a drive-by cleanup

- **File:** `/usr/src/app/clients/telegram_bot.py`
- **Dimension:** Readability
- **Description:** The diff removes `import asyncio` which was unused before this change. This is a correct cleanup but unrelated to the bug fix. Harmless, just noting it.

#### N2: Test helper `_make_update` duplicated across test files

- **File:** `/usr/src/app/tests/unit/test_telegram_unknown_commands.py:7-16`, `/usr/src/app/tests/unit/test_telegram_no_json_leak.py:7-16`
- **Dimension:** Maintainability
- **Description:** The `_make_update`, `_make_context`, and `_make_lock` helper functions are duplicated between `test_telegram_unknown_commands.py` and `test_telegram_no_json_leak.py`. These could be extracted to `conftest.py` as shared fixtures.
- **Suggested Fix:** Move shared test helpers to `conftest.py`. Low priority.

## Remediation Plan

> No critical or major fixes are required. The minor items (M1, M2) can be tracked as follow-up cards if desired. The implementation is correct and complete for the stated acceptance criteria.
