"""
Hive Mind Group Chat Telegram Bot.

Routes messages through group sessions for multi-mind conversations.
Streams responses in-place with per-mind attribution labels.
"""

import json
import logging
import os
import re
import time

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

TELEGRAM_MSG_LIMIT = 4096
SERVER_URL = os.environ.get("HIVE_MIND_SERVER_URL", f"http://localhost:{config.server_port}")

# chat_id -> group_session_id
_active_group_sessions: dict[int, str] = {}

http: aiohttp.ClientSession | None = None


# ---------------------------------------------------------------------------
# Auth / HTTP helpers
# ---------------------------------------------------------------------------

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


async def _auth_check(update: Update) -> bool:
    if not _is_allowed_user(update.effective_user.id):
        await update.message.reply_text("Not authorized.")
        return False
    return True


async def _ensure_http() -> aiohttp.ClientSession:
    global http
    if http is None or http.closed:
        http = aiohttp.ClientSession()
    return http


# ---------------------------------------------------------------------------
# Group session management
# ---------------------------------------------------------------------------

async def _get_or_create_group_session(chat_id: int) -> str:
    if chat_id in _active_group_sessions:
        return _active_group_sessions[chat_id]
    session = await _ensure_http()
    async with session.post(
        f"{SERVER_URL}/group-sessions",
        json={"moderator_mind_id": "ada"},
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
        group_session_id = data["id"]
        _active_group_sessions[chat_id] = group_session_id
        log.info("Created group session %s for chat %d", group_session_id, chat_id)
        return group_session_id


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _strip_markdown(text: str) -> str:
    """Remove markdown syntax, leaving plain readable text."""
    text = re.sub(r"```[^\n]*\n(.*?)```", r"\1", text, flags=re.DOTALL)
    saved: list[str] = []
    def _save(m: re.Match) -> str:
        saved.append(m.group(1))
        return f"\x00CODE{len(saved) - 1}\x00"
    text = re.sub(r"`([^`]+)`", _save, text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*{1,3}([^*\n]+)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}([^_\n]+)_{1,3}", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)
    for i, content in enumerate(saved):
        text = text.replace(f"\x00CODE{i}\x00", content)
    return text.strip()


def _chunk_message(text: str) -> list[str]:
    if len(text) <= TELEGRAM_MSG_LIMIT:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:TELEGRAM_MSG_LIMIT])
        text = text[TELEGRAM_MSG_LIMIT:]
    return chunks


def _parse_mind_sections(text: str) -> dict[str, str]:
    """Parse **MindName:** attribution markers from Ada's output into per-mind sections.

    Ada labels her own voice and relayed voices in the format:
        **Ada:** some text
        **Nagatha:** some other text

    Returns ordered dict of {mind_id_lower: text}.
    Falls back to {"ada": text} if no markers found.
    """
    pattern = re.compile(r"\*\*([A-Za-z]+):\*\*\s*", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if not matches:
        return {"ada": text}
    result: dict[str, str] = {}
    for i, match in enumerate(matches):
        mind_name = match.group(1).lower()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section = text[start:end].strip()
        if section:
            result[mind_name] = result.get(mind_name, "") + ("\n\n" if mind_name in result else "") + section
    return result or {"ada": text}


def _build_preview(accumulated: dict[str, str]) -> str:
    """Combined in-progress preview with attribution headers."""
    parts = []
    for mind_id, text in accumulated.items():
        parts.append(f"{mind_id.capitalize()}:\n{_strip_markdown(text)}")
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Streaming group response
# ---------------------------------------------------------------------------

async def _stream_group_response(chat_id: int, content: str, update: Update) -> None:
    """Post a placeholder, stream SSE, edit in-place every 2s, finalize."""
    try:
        group_session_id = await _get_or_create_group_session(chat_id)
    except Exception as exc:
        log.exception("Failed to get/create group session")
        await update.message.reply_text(f"Error starting session: {exc}")
        return

    placeholder = await update.message.reply_text("…")
    accumulated: dict[str, str] = {}  # mind_id -> accumulated text
    last_edit = 0.0

    try:
        session = await _ensure_http()
        timeout = aiohttp.ClientTimeout(total=0, sock_read=300)
        async with session.post(
            f"{SERVER_URL}/group-sessions/{group_session_id}/message",
            json={"content": content},
            timeout=timeout,
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                await placeholder.edit_text(f"Server error {resp.status}: {body[:200]}")
                return

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
                    if not isinstance(event, dict):
                        continue

                    if event.get("type") == "assistant":
                        mind_id = event.get("mind_id", "unknown")
                        content_blocks = event.get("message", {}).get("content", [])
                        text = ""
                        if isinstance(content_blocks, list):
                            text = "".join(
                                b.get("text", "") for b in content_blocks
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        if text:
                            accumulated[mind_id] = accumulated.get(mind_id, "") + text
                            now = time.monotonic()
                            if now - last_edit >= 2.0:
                                preview = _build_preview(accumulated)
                                try:
                                    await placeholder.edit_text(preview[:TELEGRAM_MSG_LIMIT])
                                except Exception:
                                    pass
                                last_edit = now

    except Exception as exc:
        log.exception("Error streaming group message")
        try:
            await placeholder.edit_text(f"Error: {exc}")
        except Exception:
            await update.message.reply_text(f"Error: {exc}")
        return

    # Final render — parse **MindName:** markers from Ada's output to split per mind
    if not accumulated:
        await placeholder.edit_text("(no response from the hive)")
        return

    # Merge all accumulated text (usually just "ada"), then parse mind sections
    full_text = "\n\n".join(accumulated.values())
    minds = list(_parse_mind_sections(full_text).items())

    first_mind, first_text = minds[0]
    first_final = f"{first_mind.capitalize()}:\n{_strip_markdown(first_text)}"
    try:
        await placeholder.edit_text(first_final[:TELEGRAM_MSG_LIMIT])
    except Exception:
        pass

    for mind_id, text in minds[1:]:
        label = f"{mind_id.capitalize()}:\n{_strip_markdown(text)}"
        for chunk in _chunk_message(label):
            try:
                await update.message.reply_text(chunk)
            except Exception:
                log.warning("Failed to send %s response chunk", mind_id)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_check(update):
        return
    await update.message.reply_text(
        "Hive Mind group chat.\n"
        "Send any message to hear from the collective.\n\n"
        "/new — start a fresh session\n"
        "/session — show current session ID"
    )


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_check(update):
        return
    chat_id = update.effective_chat.id
    _active_group_sessions.pop(chat_id, None)
    try:
        group_session_id = await _get_or_create_group_session(chat_id)
        await update.message.reply_text(f"New group session: {group_session_id[:8]}…")
    except Exception as exc:
        log.exception("cmd_new failed")
        await update.message.reply_text(f"Error creating session: {exc}")


async def cmd_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_check(update):
        return
    chat_id = update.effective_chat.id
    session_id = _active_group_sessions.get(chat_id)
    if session_id:
        await update.message.reply_text(f"Active session: {session_id[:8]}…")
    else:
        await update.message.reply_text("No active session. Send a message to start one.")


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if not await _auth_check(update):
        return

    chat_id = update.effective_chat.id
    content = update.message.text

    await update.message.chat.send_action("typing")
    await _stream_group_response(chat_id, content, update)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    token = _get_bot_token()
    if not token:
        log.error("HIVEMIND_TELEGRAM_BOT_TOKEN not configured — cannot start bot")
        return

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("HiveMind Telegram bot starting…")
    app.run_polling()


if __name__ == "__main__":
    main()
