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


def _owner_chat_id(client=None) -> str:
    """The person this mind's own Telegram bot answers, or empty for nobody.

    A notification belongs to the owner of the mind that raised it (story
    2.1.15), and the one Telegram account that is theirs on this bot is its
    claim: a claim links the sender to the mind's owner, and the bot answers
    nobody else. A configured `TELEGRAM_OWNER_CHAT_ID` is a guess made when
    the install was set up — on a second user's mind copied from the
    operator's, it is the operator's chat.

    So the claim decides. An unclaimed bot is nobody, and so is a gateway
    that refuses to say: a refused credential or a mind id the credential
    does not hold is not evidence the configured chat is right, and
    delivering a second person's reminder to the operator is a disclosure
    and a reminder they never get. The configured chat is used only when
    the claim cannot be asked at all — no gateway named, the gateway down,
    or one that predates the claim routes — the same fallback the surfaces
    themselves keep.
    """
    from core.secrets import get_credential

    configured = get_credential("TELEGRAM_OWNER_CHAT_ID") or ""
    base = (os.getenv("COMMS_URL") or "").rstrip("/")
    mind_id = os.getenv("MIND_ID") or ""
    if not base or not mind_id:
        return configured
    try:
        import httpx
    except ImportError:
        return configured
    # The credential the mind itself presents to comms, resolved the way
    # `mind_server.comms_credential` resolves it.
    token = (os.getenv("COMMS_BEARER_TOKEN") or "").strip() or (
        os.getenv("COMMS_ADMIN_BEARER_TOKEN") or "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = f"{base}/broker/minds/{mind_id}/surface-claim"
    try:
        getter = client.get if client is not None else httpx.get
        response = getter(url, params={"surface": "telegram"},
                          headers=headers, timeout=5)
    except httpx.HTTPError:
        return configured
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code == 404 and body == {"detail": "Not Found"}:
        # FastAPI's own answer for a path it has no route for: a gateway
        # from before claims existed. A refused claim says "No such claim".
        return configured
    if response.status_code != 200 or not isinstance(body, dict):
        return ""
    return str(body.get("claimed_ref") or "")


def _bot_token() -> str:
    """This mind's own Telegram bot token, by the rule the surfaces read.

    The claim names a person on this mind's own bot, and Telegram refuses a
    bot writing to someone who never started it, so the message has to go
    out through that bot: `TELEGRAM_BOT_TOKEN_KEYRING_KEY` in a container,
    `TELEGRAM_BOT_TOKEN` elsewhere.
    """
    try:
        from hive_surfaces import token_store

        token = token_store.resolve_token("telegram")
        if token:
            return token
    except Exception:  # noqa: BLE001 - a host without the surfaces package
        pass
    from core.secrets import get_credential

    return get_credential("TELEGRAM_BOT_TOKEN") or ""


def _telegram_direct(message: str) -> tuple[bool, str]:
    """Send via Telegram Bot API directly, to this mind's own person."""
    from core.secrets import get_credential

    token = _bot_token()
    chat_id = _owner_chat_id()

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
    # A report a mind sends its person on its own - a chief of staff woken
    # because a session stopped - arrived as text with no voice while its
    # replies to a message were spoken (Daniel, 2026-11-23). Opt-in, because
    # most callers of `send` are scripts raising an alert, and nobody asked
    # for those to be read aloud. Only after the text has landed on
    # Telegram: a voice note with no message beside it says nothing useful.
    if getattr(args, "voice", False) and results.get("telegram", {}).get("success"):
        _speak_detached(args.message)
    return 0 if delivered else 1


def _voice_server_url(voice_id: str) -> str:
    """The voice server that speaks for this mind, per the mind's own record.

    Which engine a mind uses is a field on the mind, so the URL is resolved
    from it rather than from this process's environment — a host running both
    engines would otherwise speak every notification through whichever server
    its `VOICE_SERVER_URL` happened to name.
    """
    legacy = os.getenv("VOICE_SERVER_URL", "http://voice-server:8422")
    # A mind's own container is given no voice server addresses at all; only
    # its bot is. So the fallback above is the stack's Chatterbox service,
    # and a mind that speaks through Kokoro had every notification sent to
    # an engine holding no voice for it, which answered 400 into a discarded
    # response (found 2026-11-23: the chief of staff's reports never
    # spoke). Where nothing is configured, the engines are at the stack's
    # own service names, exactly as the fallback already assumes for one of
    # them. A host that names its server keeps what it named.
    unconfigured = "VOICE_SERVER_URL" not in os.environ
    stack = {"chatterbox": "http://voice-server:8422",
             "kokoro": "http://voice-server-kokoro:8422"} if unconfigured else {}
    try:
        from hive_surfaces import voice_routing
    except ImportError:
        # A host without the surfaces package installed keeps the single
        # server it has always used. One missing import must not cost the
        # operator a spoken alert.
        return legacy
    resolver = voice_routing.VoiceServerResolver(
        os.getenv("COMMS_URL", ""),
        os.getenv("COMMS_BEARER_TOKEN", ""),
        {
            voice_routing.CHATTERBOX: os.getenv(
                "VOICE_SERVER_URL_CHATTERBOX", "") or stack.get("chatterbox", ""),
            voice_routing.KOKORO: os.getenv(
                "VOICE_SERVER_URL_KOKORO", "") or stack.get("kokoro", ""),
        },
        fallback_url=legacy,
    )
    return resolver.resolve(voice_id) or legacy


def _speak_detached(message: str) -> bool:
    """Queue `message` as a voice note to this mind's person; False when
    there is nobody to send it to.

    Forks: the caller returns at once, and the child synthesises and sends
    with no timeout pressure. Best effort by design. The text is the
    delivery, so a voice server that is down costs the sound and nothing
    else, and is never reported as a failed notification.
    """
    import httpx

    token = _bot_token()
    chat_id = _owner_chat_id()
    if not token or not chat_id:
        return False
    voice_id = os.getenv("MIND_ID", "default")
    voice_url = _voice_server_url(voice_id)

    sys.stdout.flush()
    if os.fork() != 0:
        return True

    # Child process
    os.setsid()
    try:
        tts_resp = httpx.post(
            f"{voice_url}/tts", json={"text": message, "voice_id": voice_id}, timeout=None,
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


def cmd_voice(args: argparse.Namespace) -> int:
    if args.test_mode:
        print(json.dumps({"success": True, "detail": "Voice message sent (test)"}))
        return 0

    if not _speak_detached(args.message):
        print(json.dumps({"success": False, "error": "Missing bot token or chat ID"}))
        return 1
    print(json.dumps({"success": True, "detail": "Voice synthesis queued"}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Hive Mind notification tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    send_parser = subparsers.add_parser("send", help="Send notification")
    send_parser.add_argument("--message", required=True, help="Notification message")
    send_parser.add_argument("--channels", default="telegram,email,file",
                             help="Comma-separated channels (telegram,email,file)")
    send_parser.add_argument("--alert-file", default=None, help="Custom alert file path")
    send_parser.add_argument("--test-mode", action="store_true", help="Use mock delivery")
    send_parser.add_argument("--voice", action="store_true",
                             help="Follow a delivered Telegram message with a voice note of it")

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
