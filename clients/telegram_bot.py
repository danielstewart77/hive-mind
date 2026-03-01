"""
Hive Mind Telegram Bot.

Thin HTTP client to the gateway server (server.py).
Supports text messages and voice notes (STT/TTS via voice-server).
All Claude Code interaction flows through the gateway — no SDK dependency.
"""

import asyncio
import io
import json
import logging
import os
import re
import sys
import time

import aiohttp
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import config
from core.gateway_client import GatewayClient, get_lock, get_skills, time_ago

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("hive-mind-telegram")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TELEGRAM_MSG_LIMIT = 4096
SERVER_URL = os.environ.get("HIVE_MIND_SERVER_URL", f"http://localhost:{config.server_port}")
VOICE_SERVER_URL = os.environ.get("VOICE_SERVER_URL", "http://localhost:8422")

# Surface-specific system prompt appended when spawning Telegram sessions.
# Telegram renders plain text only; voice output is spoken aloud.
# Instruct Claude to respond conversationally — no code blocks, no markdown,
# no technical formatting. Describe code concepts in plain English instead.
TELEGRAM_SURFACE_PROMPT = (
    "You are responding via Telegram. Your responses will be spoken aloud as voice or read as plain text. "
    "CRITICAL: Do not use any special characters for formatting. No asterisks, no pound signs, no backticks, "
    "no hyphens as bullet points, no underscores for emphasis, no angle brackets, no pipes. "
    "Do not write code of any kind — no code blocks, no inline code, no command snippets. "
    "Do not use numbered or bulleted lists. "
    "Write in plain flowing sentences, like natural speech. "
    "If asked about code or technical topics, describe what it does in plain English "
    "the way you would explain it to someone out loud — no syntax, no examples, just the concept."
)

# Global HTTP session and gateway client (created at startup)
http: aiohttp.ClientSession | None = None
gateway: GatewayClient | None = None


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _is_allowed_user(user_id: int) -> bool:
    """Fail-closed: empty allowlist = no access."""
    return user_id in config.telegram_allowed_users


# ---------------------------------------------------------------------------
# Voice helpers
# ---------------------------------------------------------------------------
async def _stt(ogg_bytes: bytes) -> str:
    """POST OGG audio to voice-server /stt, return transcribed text."""
    form = aiohttp.FormData()
    form.add_field("file", ogg_bytes, filename="audio.ogg", content_type="audio/ogg")
    async with http.post(f"{VOICE_SERVER_URL}/stt", data=form) as resp:
        if resp.status != 200:
            raise RuntimeError(f"STT error {resp.status}: {await resp.text()}")
        return (await resp.json())["text"]


async def _tts(text: str) -> bytes:
    """POST text to voice-server /tts, return OGG audio bytes."""
    async with http.post(f"{VOICE_SERVER_URL}/tts", json={"text": text}) as resp:
        if resp.status != 200:
            raise RuntimeError(f"TTS error {resp.status}: {await resp.text()}")
        return await resp.read()


# ---------------------------------------------------------------------------
# Markdown stripping (Telegram renders plain text only)
# ---------------------------------------------------------------------------
def _strip_markdown(text: str) -> str:
    """Remove markdown syntax, leaving plain readable text."""
    # Fenced code blocks — preserve content, drop fences
    text = re.sub(r"```[^\n]*\n(.*?)```", r"\1", text, flags=re.DOTALL)
    # Inline code
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Headers
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Bold / italic (*** ** * ___ __ _)
    text = re.sub(r"\*{1,3}([^*\n]+)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}([^_\n]+)_{1,3}", r"\1", text)
    # Links: [label](url) → label
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    # Horizontal rules
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)
    # Table separator rows (---|---|---)
    text = re.sub(r"^\|?[\s\-:|]+\|[\s\-:|]*\|?\s*$", "", text, flags=re.MULTILINE)
    return text.strip()


# ---------------------------------------------------------------------------
# Message chunking (Telegram's 4096-char limit)
# ---------------------------------------------------------------------------
def _chunk_message(text: str) -> list[str]:
    """Split text into <=4096 char chunks."""
    if len(text) <= TELEGRAM_MSG_LIMIT:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:TELEGRAM_MSG_LIMIT])
        text = text[TELEGRAM_MSG_LIMIT:]
    return chunks


# ---------------------------------------------------------------------------
# Streaming helper
# ---------------------------------------------------------------------------
async def _stream_to_message(
    sent,
    user_id: int,
    chat_id: int,
    prompt: str,
    edit_interval: float = 2.0,
) -> list[str]:
    """Stream a gateway response, progressively editing sent as chunks arrive.

    Returns the final list of message chunks (after markdown stripping).
    Telegram-specific: strips markdown before each edit and final send.
    """
    accumulated = ""
    last_edit = 0.0

    async for text_chunk in gateway.query_stream(user_id, chat_id, prompt):
        accumulated += ("\n\n" if accumulated else "") + text_chunk
        now = time.monotonic()
        if now - last_edit >= edit_interval:
            preview = _chunk_message(_strip_markdown(accumulated))[0]
            try:
                await sent.edit_text(preview)
            except Exception:
                pass  # MessageNotModified or rate limit — skip this update
            last_edit = now

    if not accumulated:
        accumulated = "(No response)"

    final_chunks = _chunk_message(_strip_markdown(accumulated))
    try:
        await sent.edit_text(final_chunks[0])
    except Exception:
        pass
    return final_chunks


# ---------------------------------------------------------------------------
# Server command formatters
# ---------------------------------------------------------------------------
def _format_sessions(sessions: list[dict]) -> str:
    if not sessions:
        return "No sessions found."
    lines = ["Your Sessions:\n"]
    for i, s in enumerate(sessions, 1):
        status_icon = {"running": "\U0001f7e2", "idle": "\U0001f4a4", "closed": "\U0001f534"}.get(
            s["status"], "\u2753"
        )
        autopilot = " \U0001f916" if s.get("autopilot") else ""
        short_id = s["id"][:8]
        summary = s.get("summary", "Untitled")
        last = s.get("last_active", 0)
        ago = time_ago(last) if last else "?"
        lines.append(
            f"{i}. {status_icon}{autopilot} {short_id} \u2014 \"{summary}\" [{s.get('model', '?')}] ({ago})"
        )
    lines.append("\n/switch <number> \u00b7 /new to start \u00b7 /kill <number> to kill")
    return "\n".join(lines)


def _format_status(data: dict) -> str:
    return (
        f"Server port: {data.get('server_port')}\n"
        f"Default model: {data.get('default_model')}\n"
        f"Sessions: {data.get('running_sessions')}/{data.get('total_sessions')} running"
    )


# ---------------------------------------------------------------------------
# Server command dispatcher
# ---------------------------------------------------------------------------
SERVER_COMMANDS = {"/clear", "/model", "/autopilot", "/kill", "/status", "/sessions", "/switch", "/new"}


async def _handle_server_command(content: str, user_id: int, chat_id: int) -> str:
    parts = content.split()
    cmd = parts[0]
    result = await gateway.server_command(user_id, chat_id, content)

    if "error" in result:
        return f"Error: {result['error']}"

    if cmd == "/sessions":
        return _format_sessions(result)
    if cmd == "/status":
        return _format_status(result)
    if cmd == "/new":
        return f"New session: {result.get('id', '?')[:8]}"
    if cmd == "/clear":
        return f"Session cleared. New: {result.get('id', '?')[:8]}"
    if cmd == "/model":
        if isinstance(result, list):
            lines = ["Available models:"]
            for m in result:
                lines.append(f"- {m['name']} ({m['provider']})")
            lines.append("\n/model <name> to switch")
            return "\n".join(lines)
        msg = f"Switched to {result.get('model')}"
        if result.get("warning"):
            msg += f"\n\u26a0\ufe0f {result['warning']}"
        return msg
    if cmd == "/autopilot":
        on = result.get("autopilot", False)
        summary = result.get("summary", "this session")
        if on:
            return f"\U0001f916 Autopilot ON for \"{summary}\""
        return f"\U0001f512 Autopilot OFF for \"{summary}\""
    if cmd == "/switch":
        return f"Resumed \"{result.get('summary', '?')}\""
    if cmd == "/kill":
        return f"Killed \"{result.get('summary', '?')}\" (status: {result.get('status')})"

    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Auth guard helper
# ---------------------------------------------------------------------------
async def _auth_check(update: Update) -> bool:
    if not _is_allowed_user(update.effective_user.id):
        await update.message.reply_text("Not authorized.")
        return False
    return True


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    msg = await _handle_server_command("/sessions", update.effective_user.id, update.effective_chat.id)
    await update.message.reply_text(msg)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    msg = await _handle_server_command("/new", update.effective_user.id, update.effective_chat.id)
    await update.message.reply_text(msg)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    msg = await _handle_server_command("/clear", update.effective_user.id, update.effective_chat.id)
    await update.message.reply_text(msg)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    msg = await _handle_server_command("/status", update.effective_user.id, update.effective_chat.id)
    await update.message.reply_text(msg)


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    name = " ".join(context.args) if context.args else None
    cmd = f"/model {name}" if name else "/model"
    msg = await _handle_server_command(cmd, update.effective_user.id, update.effective_chat.id)
    await update.message.reply_text(msg)


async def cmd_autopilot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    msg = await _handle_server_command("/autopilot", update.effective_user.id, update.effective_chat.id)
    await update.message.reply_text(msg)


async def cmd_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    target = " ".join(context.args) if context.args else ""
    if not target:
        await update.message.reply_text("Usage: /switch <number>")
        return
    msg = await _handle_server_command(f"/switch {target}", update.effective_user.id, update.effective_chat.id)
    await update.message.reply_text(msg)


async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    target = " ".join(context.args) if context.args else ""
    if not target:
        await update.message.reply_text("Usage: /kill <number>")
        return
    msg = await _handle_server_command(f"/kill {target}", update.effective_user.id, update.effective_chat.id)
    await update.message.reply_text(msg)


async def cmd_skills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    skills = get_skills()
    if not skills:
        await update.message.reply_text("No skills found.")
        return
    lines = ["Available Skills\n"]
    for s in skills:
        hint = f" {s['argument_hint']}" if s["argument_hint"] else ""
        lines.append(f"\u2022 {s['name']}{hint} \u2014 {s['description']}")
    await update.message.reply_text("\n".join(lines))


async def cmd_skill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /skill <name> [args]")
        return
    name = context.args[0]
    args = " ".join(context.args[1:]) if len(context.args) > 1 else None
    prompt = f"/{name} {args}" if args else f"/{name}"

    chat_id = update.effective_chat.id
    lock = get_lock(chat_id)
    async with lock:
        sent = await update.message.reply_text("\u2026")
        final_chunks = await _stream_to_message(sent, update.effective_user.id, chat_id, prompt)
        for extra in final_chunks[1:]:
            await update.effective_chat.send_message(extra)


# ---------------------------------------------------------------------------
# Text message handler
# ---------------------------------------------------------------------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed_user(update.effective_user.id):
        return

    # In group chats, only respond to @mentions
    if update.effective_chat.type != "private":
        bot_username = context.bot.username
        if not (update.message.text and f"@{bot_username}" in update.message.text):
            return

    content = update.message.text or ""
    if update.effective_chat.type != "private" and context.bot.username:
        content = content.replace(f"@{context.bot.username}", "").strip()

    if not content:
        return

    chat_id = update.effective_chat.id
    lock = get_lock(chat_id)

    if lock.locked():
        await update.message.reply_text("Still processing your previous message, please wait.")
        return

    async with lock:
        try:
            sent = await update.message.reply_text("\u2026")
            final_chunks = await _stream_to_message(sent, update.effective_user.id, chat_id, content)
            for extra in final_chunks[1:]:
                await update.effective_chat.send_message(extra)
        except Exception:
            log.exception("Error processing message in chat %s", chat_id)
            await update.message.reply_text("Something went wrong. Try again or use /clear.")


# ---------------------------------------------------------------------------
# Voice message handler
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# HITL approve/deny handlers
# ---------------------------------------------------------------------------
async def cmd_hitl_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed_user(update.effective_user.id):
        return
    await _handle_hitl_response(update, approved=True)


async def cmd_hitl_deny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed_user(update.effective_user.id):
        return
    await _handle_hitl_response(update, approved=False)


async def _handle_hitl_response(update: Update, approved: bool):
    """Extract token from /approve_<token> or /deny_<token> and POST to gateway."""
    text = (update.message.text or "").strip()
    # Extract token: everything after /approve_ or /deny_
    parts = text.split("_", 1)
    if len(parts) < 2 or not parts[1]:
        await update.message.reply_text("Invalid approval token.")
        return

    token = parts[1]
    hitl_secret = config.hitl_internal_token

    if not hitl_secret:
        await update.message.reply_text("HITL not configured on server.")
        return

    try:
        async with http.post(
            f"{SERVER_URL}/hitl/respond",
            json={"token": token, "approved": approved},
            headers={"X-HITL-Internal": hitl_secret},
        ) as resp:
            data = await resp.json()
            if resp.status == 200:
                verdict = "Approved" if approved else "Denied"
                await update.message.reply_text(f"{verdict}.")
            else:
                await update.message.reply_text(f"Failed: {data.get('error', 'unknown error')}")
    except Exception:
        log.exception("HITL respond failed")
        await update.message.reply_text("Failed to send response to server.")


# ---------------------------------------------------------------------------
# Voice message handler
# ---------------------------------------------------------------------------
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed_user(update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    lock = get_lock(chat_id)

    if lock.locked():
        await update.message.reply_text("Still processing your previous message, please wait.")
        return

    async with lock:
        try:
            # Download OGG/Opus from Telegram
            voice_file = await update.message.voice.get_file()
            ogg_bytes = bytes(await voice_file.download_as_bytearray())

            # STT via voice-server
            try:
                text = await _stt(ogg_bytes)
            except Exception as e:
                log.exception("STT failed in chat %s", chat_id)
                await update.message.reply_text("Couldn't transcribe your audio.")
                return

            log.info("STT: %r", text[:80])
            if not text.strip():
                await update.message.reply_text("Couldn't transcribe audio.")
                return

            # Query gateway (full response needed for TTS — no streaming display here)
            try:
                response = _strip_markdown(await gateway.query(update.effective_user.id, chat_id, text))
            except Exception as e:
                log.exception("Gateway query failed in chat %s", chat_id)
                await update.message.reply_text("Something went wrong getting a response.")
                return

            # TTS via voice-server
            try:
                ogg_response = await _tts(response)
            except Exception as e:
                log.exception("TTS failed in chat %s", chat_id)
                await update.message.reply_text("Got a response but couldn't synthesize audio.")
                return

            # Reply with voice note; fall back to text if voice delivery fails
            try:
                await update.message.reply_voice(voice=io.BytesIO(ogg_response))
            except Exception:
                log.exception("reply_voice failed for chat %s, falling back to text", chat_id)
                await update.message.reply_text(response)

        except Exception:
            log.exception("Unexpected error in voice handler for chat %s", chat_id)
            await update.message.reply_text("Something went wrong with voice processing.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _get_bot_token() -> str:
    """Load Telegram bot token — keyring first, env fallback."""
    try:
        import keyring
        token = keyring.get_password("hive-mind", "TELEGRAM_BOT_TOKEN")
        if token:
            return token
    except Exception:
        pass
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        log.error("TELEGRAM_BOT_TOKEN not found in keyring or environment.")
        sys.exit(1)
    return token


async def _on_startup(app) -> None:
    global http, gateway
    http = aiohttp.ClientSession()
    gateway = GatewayClient(http, SERVER_URL, "telegram", surface_prompt=TELEGRAM_SURFACE_PROMPT)
    log.info(
        "Hive Mind Telegram bot started (gateway=%s, voice=%s)",
        SERVER_URL,
        VOICE_SERVER_URL,
    )
    log.info("Allowed users: %s", config.telegram_allowed_users)


async def _on_shutdown(app) -> None:
    if http:
        await http.close()


if __name__ == "__main__":
    token = _get_bot_token()

    app = (
        ApplicationBuilder()
        .token(token)
        .concurrent_updates(True)
        .post_init(_on_startup)
        .post_shutdown(_on_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("autopilot", cmd_autopilot))
    app.add_handler(CommandHandler("switch", cmd_switch))
    app.add_handler(CommandHandler("kill", cmd_kill))
    app.add_handler(CommandHandler("skills", cmd_skills))
    app.add_handler(CommandHandler("skill", cmd_skill))
    app.add_handler(MessageHandler(filters.Regex(r"^/approve_\w+$"), cmd_hitl_approve))
    app.add_handler(MessageHandler(filters.Regex(r"^/deny_\w+$"), cmd_hitl_deny))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    app.run_polling()
