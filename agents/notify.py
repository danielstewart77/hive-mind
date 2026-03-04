"""
Hive Mind — Notification tools.

Sends owner notifications via multiple channels with automatic fallback.
Includes voice message delivery via TTS + Telegram sendVoice.

Channel order: telegram_direct → email (if configured) → alert_file
"""

import json
import os
import datetime
from pathlib import Path

import httpx
from agent_tooling import tool
from agents.secret_manager import get_credential
from config import config

_ALERT_FILE = Path("/usr/src/app/data/alerts.log")


def _telegram_direct(message: str) -> tuple[bool, str]:
    """Send via Telegram Bot API directly (bypasses gateway)."""
    token = get_credential("TELEGRAM_BOT_TOKEN")
    chat_id = get_credential("TELEGRAM_OWNER_CHAT_ID")

    if not token or not chat_id:
        return False, "TELEGRAM_BOT_TOKEN or TELEGRAM_OWNER_CHAT_ID not set"

    try:
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


def _alert_file(message: str) -> tuple[bool, str]:
    """Write to alert log file — always available while filesystem is up."""
    try:
        _ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now().isoformat()
        with open(_ALERT_FILE, "a") as f:
            f.write(f"[{timestamp}] {message}\n")
        return True, f"written to {_ALERT_FILE}"
    except Exception as e:
        return False, f"File error: {type(e).__name__}"


@tool(tags=["system"])
def notify_owner(message: str, channels: str = "telegram,email,file") -> str:
    """Send a notification to the owner via fallback channels.

    Tries each channel in order until one succeeds. Use this for system alerts,
    health warnings, or any notification when normal Telegram/Discord may be down.

    Args:
        message: The notification message to send.
        channels: Comma-separated channels to try in order.
                  Options: telegram, email, file
                  Default: "telegram,email,file"

    Returns:
        JSON with delivery status per channel attempted.
    """
    channel_list = [c.strip() for c in channels.split(",")]
    results = {}

    handlers = {
        "telegram": _telegram_direct,
        "email": _smtp_email,
        "file": _alert_file,
    }

    for channel in channel_list:
        if channel not in handlers:
            results[channel] = {"success": False, "detail": "unknown channel"}
            continue

        success, detail = handlers[channel](message)
        results[channel] = {"success": success, "detail": detail}

        if success:
            break  # Stop at first successful delivery

    delivered = any(r["success"] for r in results.values())
    return json.dumps({
        "delivered": delivered,
        "channels": results,
    })


@tool(tags=["system"])
def send_voice_message(message: str) -> str:
    """Send a voice message to Daniel via Telegram.

    Converts text to speech via the voice server, then sends the audio
    as a Telegram voice message. Use this for reminders or any notification
    that should be spoken aloud.

    Args:
        message: The text to speak and send as a voice message.

    Returns:
        JSON with delivery status.
    """
    token = get_credential("TELEGRAM_BOT_TOKEN")
    chat_id = config.telegram_owner_chat_id
    voice_url = os.getenv("VOICE_SERVER_URL", "http://voice-server:8422")

    if not token or not chat_id:
        return json.dumps({"success": False, "error": "Missing bot token or chat ID"})

    try:
        tts_resp = httpx.post(
            f"{voice_url}/tts", json={"text": message}, timeout=30,
        )
        if tts_resp.status_code != 200:
            return json.dumps({"success": False, "error": f"TTS failed: {tts_resp.status_code}"})

        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendVoice",
            data={"chat_id": str(chat_id)},
            files={"voice": ("message.ogg", tts_resp.content, "audio/ogg")},
            timeout=15,
        )
        if resp.status_code == 200:
            return json.dumps({"success": True, "detail": "Voice message sent"})
        return json.dumps({"success": False, "error": f"Telegram API: {resp.status_code}"})
    except Exception as e:
        return json.dumps({"success": False, "error": f"{type(e).__name__}: {e}"})
