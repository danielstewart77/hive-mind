"""
Hive Mind Group Chat Telegram Bot.

Routes messages through group sessions for multi-mind conversations.
Streams responses in-place with per-mind attribution labels.
Supports voice (STT/TTS per mind), photos, HITL, and message queuing.
"""

import base64
import io
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
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import config
from core.gateway_client import get_lock, get_queue

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("hive-mind-hivemind-bot")

TELEGRAM_MSG_LIMIT = 4096
SERVER_URL = os.environ.get("HIVE_MIND_SERVER_URL", f"http://localhost:{config.server_port}")
VOICE_SERVER_URL = os.environ.get("VOICE_SERVER_URL", "http://localhost:8422")

# Surface prompt: tells the minds they're in a Telegram group chat.
GROUP_SURFACE_PROMPT = (
    "You are responding in a Telegram group chat with multiple AI minds. "
    "Write in plain flowing sentences — no markdown formatting, no asterisks, "
    "no code blocks, no bullet points. Natural conversational prose only."
)

# chat_id -> group_session_id
_active_group_sessions: dict[int, str] = {}

http: aiohttp.ClientSession | None = None


# ---------------------------------------------------------------------------
# Auth helpers
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


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

async def _ensure_http() -> aiohttp.ClientSession:
    global http
    if http is None or http.closed:
        http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=0, sock_read=0))
    return http


# ---------------------------------------------------------------------------
# Voice helpers
# ---------------------------------------------------------------------------

async def _stt(ogg_bytes: bytes) -> str:
    """POST OGG audio to voice-server /stt, return transcribed text."""
    session = await _ensure_http()
    form = aiohttp.FormData()
    form.add_field("file", ogg_bytes, filename="audio.ogg", content_type="audio/ogg")
    async with session.post(f"{VOICE_SERVER_URL}/stt", data=form) as resp:
        if resp.status != 200:
            raise RuntimeError(f"STT error {resp.status}: {await resp.text()}")
        return (await resp.json())["text"]


async def _tts(text: str, voice_id: str = "default") -> bytes:
    """POST text to voice-server /tts, return OGG audio bytes.

    voice_id maps to voice_ref/{voice_id}.wav on the voice server.
    Falls back to default.wav if the file does not exist.
    """
    session = await _ensure_http()
    async with session.post(f"{VOICE_SERVER_URL}/tts", json={"text": text, "voice_id": voice_id}) as resp:
        if resp.status != 200:
            raise RuntimeError(f"TTS error {resp.status}: {await resp.text()}")
        return await resp.read()


# ---------------------------------------------------------------------------
# Group session management
# ---------------------------------------------------------------------------

async def _get_or_create_group_session(chat_id: int) -> str:
    if chat_id in _active_group_sessions:
        return _active_group_sessions[chat_id]
    session = await _ensure_http()
    async with session.post(
        f"{SERVER_URL}/group-sessions",
        json={"moderator_mind_id": "ada", "surface_prompt": GROUP_SURFACE_PROMPT},
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
    text = re.sub(r"^\|?[\s\-:|]+\|[\s\-:|]*\|?\s*$", "", text, flags=re.MULTILINE)
    for i, content in enumerate(saved):
        text = text.replace(f"\x00CODE{i}\x00", content)
    return text.strip()


def _looks_like_json(text: str) -> bool:
    stripped = text.strip()
    if not stripped or not (stripped.startswith("{") or stripped.startswith("[")):
        return False
    try:
        parsed = json.loads(stripped)
        return isinstance(parsed, (dict, list))
    except (json.JSONDecodeError, ValueError):
        return False


def _sanitize_response(text: str) -> str:
    if _looks_like_json(text):
        return "Done."
    return text


def _chunk_message(text: str) -> list[str]:
    if len(text) <= TELEGRAM_MSG_LIMIT:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:TELEGRAM_MSG_LIMIT])
        text = text[TELEGRAM_MSG_LIMIT:]
    return chunks


def _format_queue_batch(messages: list[str]) -> str:
    if len(messages) == 1:
        return messages[0]
    items = "\n".join(f"{i + 1}. {m}" for i, m in enumerate(messages))
    return (
        "While you were processing my previous message, I sent several more. "
        "Please address all of them in one reply:\n\n" + items
    )


def _parse_mind_sections(text: str) -> dict[str, str]:
    """Parse **MindName:** attribution markers from Ada's output into per-mind sections."""
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
    parts = []
    for mind_id, text in accumulated.items():
        parts.append(f"{mind_id.capitalize()}:\n{_strip_markdown(text)}")
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Streaming group response
# ---------------------------------------------------------------------------

async def _stream_group_response(
    chat_id: int,
    content: str,
    update: Update,
    images: list[dict] | None = None,
) -> dict[str, str] | None:
    """Post a placeholder, stream SSE, edit in-place every 2s, finalize."""
    try:
        group_session_id = await _get_or_create_group_session(chat_id)
    except Exception as exc:
        log.exception("Failed to get/create group session")
        await update.message.reply_text(f"Error starting session: {exc}")
        return None

    placeholder = await update.message.reply_text("…")
    accumulated: dict[str, str] = {}
    last_edit = 0.0

    try:
        session = await _ensure_http()
        payload: dict = {"content": content}
        if images:
            payload["images"] = images
        timeout = aiohttp.ClientTimeout(total=0, sock_read=300)
        async with session.post(
            f"{SERVER_URL}/group-sessions/{group_session_id}/message",
            json=payload,
            timeout=timeout,
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                await placeholder.edit_text(f"Server error {resp.status}: {body[:200]}")
                return None

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
        return None

    if not accumulated:
        await placeholder.edit_text("(no response from the hive)")
        return None

    full_text = "\n\n".join(accumulated.values())
    minds = list(_parse_mind_sections(full_text).items())

    first_mind, first_text = minds[0]
    first_final = _sanitize_response(f"{first_mind.capitalize()}:\n{_strip_markdown(first_text)}")
    try:
        await placeholder.edit_text(first_final[:TELEGRAM_MSG_LIMIT])
    except Exception:
        pass

    for mind_id, text in minds[1:]:
        label = _sanitize_response(f"{mind_id.capitalize()}:\n{_strip_markdown(text)}")
        for chunk in _chunk_message(label):
            try:
                await update.message.reply_text(chunk)
            except Exception:
                log.warning("Failed to send %s response chunk", mind_id)

    return dict(minds)


# ---------------------------------------------------------------------------
# HITL inline keyboard callback handler
# ---------------------------------------------------------------------------

async def handle_hitl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button taps for HITL approve/deny."""
    query = update.callback_query

    if not _is_allowed_user(query.from_user.id):
        await query.answer("Not authorized.")
        return

    data = query.data or ""
    if data.startswith("hitl_approve_"):
        action, token = "approve", data[len("hitl_approve_"):]
    elif data.startswith("hitl_deny_"):
        action, token = "deny", data[len("hitl_deny_"):]
    else:
        await query.answer("Unknown action.")
        return

    try:
        session = await _ensure_http()
        async with session.post(
            f"{SERVER_URL}/hitl/respond",
            json={"token": token, "approved": action == "approve"},
            headers={"X-HITL-Internal": config.hitl_internal_token},
        ) as resp:
            original_text = query.message.text or ""
            body = original_text.split("\n\n", 1)[-1] if "\n\n" in original_text else original_text
            if resp.status == 200:
                prefix = "✅ Approved" if action == "approve" else "❌ Denied"
            else:
                prefix = "⏰ Expired"
            await query.edit_message_text(f"{prefix}\n\n{body}")
            await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        log.exception("HITL callback handler failed")

    await query.answer()


# ---------------------------------------------------------------------------
# Command handlers
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


# ---------------------------------------------------------------------------
# Text message handler
# ---------------------------------------------------------------------------

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if not await _auth_check(update):
        return

    content = update.message.text
    chat_id = update.effective_chat.id
    lock = get_lock(chat_id)
    queue = get_queue(chat_id)

    if lock.locked():
        pos = queue.qsize() + 1
        await queue.put(content)
        await update.message.reply_text(f"Still processing — yours is queued (#{pos}).")
        return

    async with lock:
        try:
            await update.message.chat.send_action("typing")
            await _stream_group_response(chat_id, content, update)
        except Exception:
            log.exception("Error processing message in chat %s", chat_id)
            await update.message.reply_text("Something went wrong. Try /new to reset.")

        while not queue.empty():
            queued: list[str] = []
            while not queue.empty():
                queued.append(queue.get_nowait())
            batch = _format_queue_batch(queued)
            try:
                await update.effective_chat.send_action("typing")
                await _stream_group_response(chat_id, batch, update)
            except Exception:
                log.exception("Error processing queued batch in chat %s", chat_id)
                await update.effective_chat.send_message("Something went wrong processing your queued messages.")


# ---------------------------------------------------------------------------
# Photo handler
# ---------------------------------------------------------------------------

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_check(update):
        return

    caption = update.message.caption or "Please analyze this image."
    chat_id = update.effective_chat.id
    lock = get_lock(chat_id)
    queue = get_queue(chat_id)

    try:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        photo_bytes = bytes(await file.download_as_bytearray())
        b64_data = base64.b64encode(photo_bytes).decode("ascii")
        images = [{"media_type": "image/jpeg", "data": b64_data}]
    except Exception:
        log.exception("Photo download failed in chat %s", chat_id)
        await update.message.reply_text("Couldn't download your image.")
        return

    if lock.locked():
        await update.message.reply_text("Still processing — image will follow.")

    async with lock:
        try:
            await update.message.chat.send_action("typing")
            await _stream_group_response(chat_id, caption, update, images=images)
        except Exception:
            log.exception("Error processing photo in chat %s", chat_id)
            await update.message.reply_text("Something went wrong processing your image.")


# ---------------------------------------------------------------------------
# Voice handler
# ---------------------------------------------------------------------------

async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not await _auth_check(update):
        return

    # STT outside the lock so we have text to queue if busy
    try:
        voice_file = await update.message.voice.get_file()
        ogg_bytes = bytes(await voice_file.download_as_bytearray())
        text = await _stt(ogg_bytes)
    except Exception:
        log.exception("STT failed")
        await update.message.reply_text("Couldn't transcribe your audio.")
        return

    if not text.strip():
        await update.message.reply_text("Couldn't transcribe audio.")
        return

    log.info("STT: %r", text[:80])
    chat_id = update.effective_chat.id
    lock = get_lock(chat_id)
    queue = get_queue(chat_id)

    if lock.locked():
        pos = queue.qsize() + 1
        await queue.put(text)
        await update.message.reply_text(f"Still processing — yours is queued (#{pos}).")
        return

    async with lock:
        try:
            await update.message.chat.send_action("typing")
            minds = await _stream_group_response(chat_id, text, update)
            if minds:
                for mind_id, mind_text in minds.items():
                    stripped = _strip_markdown(mind_text).strip()
                    if not stripped:
                        continue
                    try:
                        ogg = await _tts(stripped, voice_id=mind_id)
                        await update.effective_chat.send_voice(voice=io.BytesIO(ogg))
                    except Exception:
                        log.warning("Voice TTS/send failed for %s", mind_id, exc_info=True)
        except Exception:
            log.exception("Unexpected error in voice handler for chat %s", chat_id)
            await update.message.reply_text("Something went wrong with voice processing.")

        while not queue.empty():
            queued: list[str] = []
            while not queue.empty():
                queued.append(queue.get_nowait())
            batch = _format_queue_batch(queued)
            try:
                await update.effective_chat.send_action("typing")
                await _stream_group_response(chat_id, batch, update)
            except Exception:
                log.exception("Error processing queued batch in chat %s", chat_id)
                await update.effective_chat.send_message("Something went wrong processing your queued messages.")


# ---------------------------------------------------------------------------
# Unknown command catch-all (routes slash commands to group session)
# ---------------------------------------------------------------------------

async def handle_unknown_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_check(update):
        return

    content = update.message.text or ""
    if ctx.bot.username:
        content = content.replace(f"@{ctx.bot.username}", "")
    content = content.strip()
    if not content:
        return

    chat_id = update.effective_chat.id
    lock = get_lock(chat_id)
    queue = get_queue(chat_id)

    if lock.locked():
        pos = queue.qsize() + 1
        await queue.put(content)
        await update.message.reply_text(f"Still processing — yours is queued (#{pos}).")
        return

    async with lock:
        try:
            await update.message.chat.send_action("typing")
            await _stream_group_response(chat_id, content, update)
        except Exception:
            log.exception("Error processing command in chat %s", chat_id)
            await update.message.reply_text("Something went wrong. Try /new to reset.")

        while not queue.empty():
            queued: list[str] = []
            while not queue.empty():
                queued.append(queue.get_nowait())
            batch = _format_queue_batch(queued)
            try:
                await _stream_group_response(chat_id, batch, update)
            except Exception:
                log.exception("Error processing queued batch in chat %s", chat_id)
                await update.effective_chat.send_message("Something went wrong processing your queued messages.")


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------

async def _on_startup(app) -> None:
    global http
    http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=0, sock_read=0))
    log.info(
        "HiveMind Telegram bot started (gateway=%s, voice=%s)",
        SERVER_URL,
        VOICE_SERVER_URL,
    )


async def _on_shutdown(app) -> None:
    if http:
        await http.close()


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
        .concurrent_updates(True)
        .post_init(_on_startup)
        .post_shutdown(_on_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CallbackQueryHandler(handle_hitl_callback, pattern=r"^hitl_(approve|deny)_"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.COMMAND, handle_unknown_command))

    log.info("HiveMind Telegram bot starting…")
    app.run_polling()


if __name__ == "__main__":
    main()
