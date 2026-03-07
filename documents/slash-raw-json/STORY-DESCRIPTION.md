# [Bug] Slash command returns raw JSON to Telegram chat

**Card ID:** 1722252456479425985

## Description

After sending a slash command via Telegram (e.g. `/remember`), the bot sends back raw JSON instead of staying silent or sending a human-readable confirmation.

The bot currently returns:
```json
{
  "status": "completed",
  "session_id": "7af0b1c0-f768-45c8-83c0-df40f4a27532"
}
```

This raw JSON response is forwarded directly to the chat, creating a poor user experience.

## Root Cause (suspected)

The slash command handler returns a completion payload that the Telegram bot forwards directly to the chat instead of discarding or formatting it.

## Acceptance Criteria

- Slash commands (`/remember`, `/plan`, etc.) no longer return raw JSON to the chat
- Commands either complete silently or send a brief, human-readable confirmation message
- No structured response payloads leak to the user-facing Telegram interface
- Session completion is internally tracked without exposing implementation details

## Tasks

1. Identify where the slash command handler returns the completion payload
2. Modify the Telegram bot's command handler to properly format or suppress the response
3. Test multiple slash commands to ensure consistent behavior
4. Verify that silent completion or brief confirmations work as intended
