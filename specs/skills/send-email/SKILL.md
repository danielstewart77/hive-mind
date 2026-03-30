---
name: send-email
description: Send an email via Gmail on Daniel's behalf using the hive-mind-mcp tool. Requires human approval via Telegram before sending. Use when Daniel asks to send an email or when an automated workflow needs to deliver an email.
user-invocable: true
---

# Send Email

## Overview

Sends email via Gmail using the `mcp__hive-mind-mcp__send_email` MCP tool. The tool requires human approval via Telegram before the message is dispatched — the HITL gate will fire automatically.

## Signature

Always append this block to the bottom of every email body sent on Daniel's behalf:

```
---
Sent on behalf of Daniel by Ada.
```

Do not append if Daniel explicitly says not to, or if the email is clearly automated/system-generated.

## Tool Call

```
mcp__hive-mind-mcp__send_email(
    to="recipient@example.com",
    subject="Subject line",
    body="...",
    cc="",                        # optional, comma-separated
    attachment_filename="",       # optional, e.g. "report.md"
    attachment_content_b64=""     # optional, base64-encoded file content
)
```

## Attachments

Pass a single attachment via the two flat string params — no JSON arrays:

```
attachment_filename="alexander-setup.md"
attachment_content_b64="<base64 content>"
```

To base64-encode a file:
```bash
base64 -w 0 /path/to/file
```

Multiple attachments are not currently supported. Send multiple emails if needed.

## Process

1. Gather: to, subject, body (and attachment fields if needed)
2. Append signature to body (unless directed otherwise)
3. Base64-encode any attachment file with `base64 -w 0`
4. Call `mcp__hive-mind-mcp__send_email` — Telegram approval will fire automatically
5. Confirm sent message ID from the response

## Error Handling

- `Gmail not authenticated` → run `setup_gmail.py` in the `hive_mind_mcp` container
- Approval denied → do not retry; report back to Daniel
