#!/usr/bin/env python3
"""Send notifications via multiple channels with fallback.

Standalone stateless tool. Dependencies: httpx.
"""

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

# Allow importing core.secrets
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

_DEFAULT_ALERT_FILE = "/usr/src/app/data/alerts.log"


def _telegram_direct(message: str) -> tuple[bool, str]:
    """Send via Telegram Bot API directly."""
    from core.secrets import get_credential

    token = get_credential("TELEGRAM_BOT_TOKEN")
    chat_id = get_credential("TELEGRAM_OWNER_CHAT_ID")

    if not token or not chat_id:
        return False, "TELEGRAM_BOT_TOKEN or TELEGRAM_OWNER_CHAT_ID not set"

    try:
        import httpx
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        if resp.status_code == 200:
            return True, "delivered via Telegram"
        return False, f"Telegram API returned {resp.status_code}"
    except Exception as e:
        return False, f"Telegram error: {type(e).__name__}"


def _smtp_email(message: str) -> tuple[bool, str]:
    """Send via SMTP if credentials are configured."""
    from core.secrets import get_credential

    smtp_host = get_credential("SMTP_HOST")
    smtp_user = get_credential("SMTP_USER")
    smtp_pass = get_credential("SMTP_PASSWORD")
    smtp_to = get_credential("SMTP_TO") or smtp_user

    if not all([smtp_host, smtp_user, smtp_pass]):
        return False, "SMTP not configured (SMTP_HOST/SMTP_USER/SMTP_PASSWORD)"

    try:
        import smtplib
        from email.mime.text import MIMEText

        smtp_port = int(get_credential("SMTP_PORT") or "587")
        msg = MIMEText(message)
        msg["Subject"] = "Hive Mind Alert"
        msg["From"] = smtp_user
        msg["To"] = smtp_to

        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        return True, f"delivered via email to {smtp_to}"
    except Exception as e:
        return False, f"SMTP error: {type(e).__name__}"


def _alert_file(message: str, alert_file_path: str) -> tuple[bool, str]:
    """Write to alert log file."""
    try:
        alert_file = Path(alert_file_path)
        alert_file.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now().isoformat()
        with open(alert_file, "a") as f:
            f.write(f"[{timestamp}] {message}\n")
        return True, f"written to {alert_file}"
    except Exception as e:
        return False, f"File error: {type(e).__name__}"


def cmd_send(args: argparse.Namespace) -> int:
    if args.test_mode:
        # Simulate successful telegram delivery
        results: dict[str, dict] = {}
        channel_list = [c.strip() for c in args.channels.split(",")]
        for channel in channel_list:
            if channel == "telegram":
                results[channel] = {"success": True, "detail": "delivered via Telegram (test)"}
                break
            elif channel == "email":
                results[channel] = {"success": True, "detail": "delivered via email (test)"}
                break
            elif channel == "file":
                results[channel] = {"success": True, "detail": "written to file (test)"}
                break
        print(json.dumps({"delivered": True, "channels": results}))
        return 0

    channel_list = [c.strip() for c in args.channels.split(",")]
    results = {}
    alert_file_path = args.alert_file or _DEFAULT_ALERT_FILE

    handlers = {
        "telegram": lambda msg: _telegram_direct(msg),
        "email": lambda msg: _smtp_email(msg),
        "file": lambda msg: _alert_file(msg, alert_file_path),
    }

    for channel in channel_list:
        if channel not in handlers:
            results[channel] = {"success": False, "detail": "unknown channel"}
            continue

        success, detail = handlers[channel](args.message)
        results[channel] = {"success": success, "detail": detail}

        if success:
            break

    delivered = any(r["success"] for r in results.values())
    print(json.dumps({"delivered": delivered, "channels": results}))
    return 0 if delivered else 1


def cmd_voice(args: argparse.Namespace) -> int:
    if args.test_mode:
        print(json.dumps({"success": True, "detail": "Voice message sent (test)"}))
        return 0

    from core.secrets import get_credential
    import httpx

    token = get_credential("TELEGRAM_BOT_TOKEN")
    chat_id = get_credential("TELEGRAM_OWNER_CHAT_ID")
    voice_url = os.getenv("VOICE_SERVER_URL", "http://voice-server:8422")

    if not token or not chat_id:
        print(json.dumps({"success": False, "error": "Missing bot token or chat ID"}))
        return 1

    # Fork: parent returns immediately so the caller is unblocked.
    # Child detaches and handles TTS synthesis + voice delivery with no timeout pressure.
    pid = os.fork()
    if pid != 0:
        print(json.dumps({"success": True, "detail": "Voice synthesis queued"}))
        return 0

    # Child process
    os.setsid()
    try:
        voice_id = os.getenv("MIND_ID", "default")
        tts_resp = httpx.post(
            f"{voice_url}/tts", json={"text": args.message, "voice_id": voice_id}, timeout=None,
        )
        if tts_resp.status_code == 200:
            httpx.post(
                f"https://api.telegram.org/bot{token}/sendVoice",
                data={"chat_id": str(chat_id)},
                files={"voice": ("message.ogg", tts_resp.content, "audio/ogg")},
                timeout=15,
            )
    except Exception:
        pass
    finally:
        os._exit(0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Hive Mind notification tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    send_parser = subparsers.add_parser("send", help="Send notification")
    send_parser.add_argument("--message", required=True, help="Notification message")
    send_parser.add_argument("--channels", default="telegram,email,file",
                             help="Comma-separated channels (telegram,email,file)")
    send_parser.add_argument("--alert-file", default=None, help="Custom alert file path")
    send_parser.add_argument("--test-mode", action="store_true", help="Use mock delivery")

    voice_parser = subparsers.add_parser("voice", help="Send voice message")
    voice_parser.add_argument("--message", required=True, help="Text to speak")
    voice_parser.add_argument("--test-mode", action="store_true", help="Use mock delivery")

    args = parser.parse_args()

    if args.command == "send":
        return cmd_send(args)
    elif args.command == "voice":
        return cmd_voice(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
