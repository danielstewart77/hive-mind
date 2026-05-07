# HITL Telegram Inline Keyboard Buttons

## Overview

Replace the current plain-text `/approve_<token>` / `/deny_<token>` HITL approval interface in Telegram with native inline keyboard buttons. One tap to approve or reject, with visual feedback on resolution and expiry.

## User Requirements

Daniel receives HITL approval requests via Telegram. Currently these arrive as plain text with clickable `/approve_` and `/deny_` commands — hard to tap on mobile and not visually clear. He wants proper tappable buttons with clear visual feedback.

## User Acceptance Criteria

- [ ] HITL approval messages display with inline keyboard buttons (Approve / Reject) below the message
- [ ] Approve and Reject buttons are on separate rows, well-spaced, easy to tap on mobile without accidentally hitting the wrong one
- [ ] Message body scales to fit the full content (e.g. complete email body, not truncated)
- [ ] Tapping Approve sends the approval and updates the message to show "Approved"
- [ ] Tapping Reject sends the denial and updates the message to show "Denied"
- [ ] If the request expires (3-min TTL), buttons disappear and message updates to show "Expired"
- [ ] Double-tapping an already-resolved button is silently ignored
- [ ] Multiple pending HITL requests are independent — each message has its own button set
- [ ] Old `/approve_` `/deny_` text commands are removed
- [ ] Existing HITL store (`core/hitl.py`) and gateway endpoints unchanged — only the Telegram presentation layer changes

## Technical Specification

### Current Flow

1. A caller (skill, hive-tools endpoint, etc.) calls `POST /hitl/request` with action + summary
2. `server.py:_send_telegram_approval_request()` sends a plain text message via Telegram Bot API with `/approve_<token>` and `/deny_<token>` as clickable commands
3. User types or taps the command in Telegram
4. `telegram_bot.py:cmd_hitl_approve/deny` regex-matches the command, POSTs to `POST /hitl/respond`
5. `hitl_store.resolve()` sets the event, unblocking the waiting caller

### New Flow

1. The caller calls `POST /hitl/request` — unchanged
2. `server.py:_send_telegram_approval_request()` sends a message with `InlineKeyboardMarkup` containing two `InlineKeyboardButton`s on **separate rows** for easy tapping:
   - Row 1: "✅ Approve" with `callback_data = f"hitl_approve_{token}"`
   - Row 2: "❌ Reject" with `callback_data = f"hitl_deny_{token}"`
3. User taps a button in Telegram
4. Telegram sends a `CallbackQuery` to the bot
5. New `telegram_bot.py:handle_hitl_callback()` handler:
   - Extracts token and action from `callback_data`
   - POSTs to `POST /hitl/respond`
   - Calls `query.edit_message_reply_markup()` to remove buttons
   - Calls `query.edit_message_text()` to append resolution status
   - Calls `query.answer()` to dismiss the loading spinner
6. `hitl_store.resolve()` sets the event — unchanged

### Expiry Handling

The gateway already runs `_hitl_cleanup_loop()` every 30s. To update Telegram messages on expiry, we need to track which messages correspond to which tokens. Options:

**Option A (simple):** Store `(chat_id, message_id)` alongside the token in a dict in `server.py`. When `cleanup_expired()` runs, iterate expired tokens and call `editMessageReplyMarkup` + `editMessageText` to show "Expired". This keeps it server-side.

**Option B (callback-only):** When a user taps a button on an expired request, the callback handler checks the token status, finds it expired, and updates the message then. No proactive expiry update — the buttons just stop working and show "Expired" on next tap.

**Recommended: Option A** — proactive expiry is better UX. Daniel shouldn't have to tap a dead button to find out it expired.

### Message Format

The approved mockup (sent to Telegram and confirmed by Daniel):

```
🔔 Approval Required

Ada wants to send an email:

To: john.smith@example.com
Subject: Meeting follow-up

Hi John,

Thanks for the call earlier. I've attached the revised scope document
with the changes we discussed. Let me know if the timeline on page 3
works for your team.

Best,
Daniel

─────────────────

[    ✅ Approve    ]      ← separate row, full-width button
[    ❌ Reject     ]      ← separate row, full-width button
```

Implementation (inline_keyboard JSON):
```json
{
    "inline_keyboard": [
        [{"text": "✅ Approve", "callback_data": "hitl_approve_{token}"}],
        [{"text": "❌ Reject", "callback_data": "hitl_deny_{token}"}]
    ]
}
```

After approval:
```
✅ Approved

Ada wants to send an email:
...
```

After denial:
```
❌ Denied

Ada wants to send an email:
...
```

After expiry:
```
⏰ Expired

Ada wants to send an email:
...
```

## Code References

| File | Action |
|------|--------|
| `server.py` | **Modify** — `_send_telegram_approval_request()`: send `reply_markup` with `InlineKeyboardMarkup`. Add message tracking dict for expiry updates. Update `_hitl_cleanup_loop()` to edit expired messages. |
| `clients/telegram_bot.py` | **Modify** — Add `CallbackQueryHandler` for `hitl_approve_*` / `hitl_deny_*` patterns. Remove old `cmd_hitl_approve`, `cmd_hitl_deny`, and their `MessageHandler` registrations. |
| `core/hitl.py` | **No changes** — store logic stays the same |

## Implementation Order

1. Modify `server.py:_send_telegram_approval_request()` to send inline keyboard markup
2. Add message tracking dict (`_hitl_messages: dict[str, tuple[chat_id, message_id]]`) in `server.py`
3. Add `handle_hitl_callback()` in `telegram_bot.py` with `CallbackQueryHandler`
4. Implement message editing on approve/deny (remove buttons, update text with status)
5. Add expiry message editing in `_hitl_cleanup_loop()`
6. Remove old `/approve_` `/deny_` regex handlers and `cmd_hitl_approve` / `cmd_hitl_deny` functions
7. Test: trigger HITL → tap Approve → verify message updates
8. Test: trigger HITL → wait 3 min → verify message shows Expired
9. Test: trigger HITL → tap Approve twice → verify second tap ignored
