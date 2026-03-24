"""
Hive Mind Group Chat Telegram Bot.

Routes messages through group sessions for multi-mind conversations.
Each mind's response is attributed by name in the chat.
"""

import json
import logging
import os

import aiohttp
import keyring
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("hive-mind-hivemind-bot")

SERVER_URL = os.environ.get("HIVE_MIND_SERVER_URL", f"http://localhost:{config.server_port}")

# chat_id -> group_session_id
_active_group_sessions: dict[int, str] = {}

http: aiohttp.ClientSession | None = None


def _get_bot_token() -> str | None:
    try:
        token = keyring.get_password("hive-mind", "HIVEMIND_TELEGRAM_BOT_TOKEN")
        if token:
            return token
    except Exception:
        pass
    return os.getenv("HIVEMIND_TELEGRAM_BOT_TOKEN")


def _is_allowed_user(user_id: int) -> bool:
    return user_id in config.telegram_allowed_users


async def _ensure_http() -> aiohttp.ClientSession:
    global http
    if http is None or http.closed:
        http = aiohttp.ClientSession()
    return http


async def _get_or_create_group_session(chat_id: int) -> str:
    if chat_id in _active_group_sessions:
        return _active_group_sessions[chat_id]
    session = await _ensure_http()
    async with session.post(
        f"{SERVER_URL}/group-sessions",
        json={"moderator_mind_id": "ada"},
    ) as resp:
        data = await resp.json()
        group_session_id = data["id"]
        _active_group_sessions[chat_id] = group_session_id
        log.info("Created group session %s for chat %d", group_session_id, chat_id)
        return group_session_id


async def _send_group_message(chat_id: int, content: str) -> list[tuple[str, str]]:
    """Send message to group session. Returns list of (mind_id, text) tuples."""
    group_session_id = await _get_or_create_group_session(chat_id)
    session = await _ensure_http()
    responses: list[tuple[str, str]] = []

    timeout = aiohttp.ClientTimeout(total=0, sock_read=0)
    async with session.post(
        f"{SERVER_URL}/group-sessions/{group_session_id}/message",
        json={"content": content},
        timeout=timeout,
    ) as resp:
        buf = ""
        async for chunk in resp.content.iter_any():
            buf += chunk.decode()
            while "\n" in buf:
                raw_line, buf = buf.split("\n", 1)
                raw_line = raw_line.strip()
                if not raw_line or not raw_line.startswith("data: "):
                    continue
                try:
                    event = json.loads(raw_line.removeprefix("data: "))
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "assistant":
                    mind_id = event.get("mind_id", "unknown")
                    msg = event.get("message", {})
                    content_blocks = msg.get("content", [])
                    if isinstance(content_blocks, list):
                        text = " ".join(
                            b.get("text", "") for b in content_blocks if b.get("type") == "text"
                        ).strip()
                    else:
                        text = str(content_blocks)
                    if text:
                        responses.append((mind_id, text))

    return responses


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed_user(update.effective_user.id):
        return
    await update.message.reply_text("Hive Mind group chat. Send a message to hear from the collective.")


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed_user(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    _active_group_sessions.pop(chat_id, None)
    group_session_id = await _get_or_create_group_session(chat_id)
    await update.message.reply_text(f"New group session started: {group_session_id[:8]}…")


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if not _is_allowed_user(update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    content = update.message.text

    try:
        responses = await _send_group_message(chat_id, content)
        if not responses:
            await update.message.reply_text("(no response from the hive)")
            return
        for _mind_id, text in responses:
            await update.message.reply_text(text)
    except Exception as exc:
        log.exception("Error sending group message")
        await update.message.reply_text(f"Error: {exc}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    token = _get_bot_token()
    if not token:
        log.error("HIVEMIND_TELEGRAM_BOT_TOKEN not configured — cannot start bot")
        return

    app = (
        ApplicationBuilder()
        .token(token)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("HiveMind Telegram bot starting…")
    app.run_polling()


if __name__ == "__main__":
    main()
