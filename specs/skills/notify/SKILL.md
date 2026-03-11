---
name: notify
description: Send notifications via Telegram, email, or file with fallback
user-invocable: false
---

# Notify Tool

Send notifications via multiple channels with automatic fallback.

## Usage

### Send notification
```bash
/usr/src/app/tools/stateless/notify/venv/bin/python /usr/src/app/tools/stateless/notify/notify.py send \
  --message "<message>" \
  --channels "telegram,email,file"
```

### Send voice message
```bash
/usr/src/app/tools/stateless/notify/venv/bin/python /usr/src/app/tools/stateless/notify/notify.py voice \
  --message "<text to speak>"
```

## Arguments

### send
- `--message` (required): Notification message text
- `--channels` (optional): Comma-separated channels to try in order (default: "telegram,email,file")

### voice
- `--message` (required): Text to convert to speech and send via Telegram

## Output

JSON with delivery status per channel attempted.
