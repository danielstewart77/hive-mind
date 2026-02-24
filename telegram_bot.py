"""
Hive Mind Telegram Bot.

Thin HTTP client to the gateway server (server.py).
Supports text messages and voice notes (STT/TTS via voice-server).
All Claude Code interaction flows through the gateway — no SDK dependency.
"""

import asyncio
import glob
import io
import json
import logging
import os
import re
import sys
from datetime import datetime

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

# Global HTTP session (created at startup)
http: aiohttp.ClientSession | None = None

# Per-chat processing locks
_locks: dict[int, asyncio.Lock] = {}

# ---------------------------------------------------------------------------
# Skills helpers
# ---------------------------------------------------------------------------
_SKILLS_DIR = os.path.expanduser("~/.claude/skills")


def _get_skills() -> list[dict]:
    """Read all user-invocable skills from SKILL.md files."""
    skills = []
    for path in sorted(glob.glob(os.path.join(_SKILLS_DIR, "*/SKILL.md"))):
        try:
            with open(path) as f:
                content = f.read()
            m = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if not m:
                continue
            fm: dict[str, str] = {}
            for line in m.group(1).split("\n"):
                if ":" in line:
                    k, _, v = line.partition(":")
                    fm[k.strip()] = v.strip().strip('"').strip("'")
            invocable = fm.get("user-invocable", fm.get("user_invocable", "")).lower()
            if invocable == "true" and (name := fm.get("name", "")):
                skills.append({
                    "name": name,
                    "description": fm.get("description", "")[:100],
                    "argument_hint": fm.get("argument-hint", ""),
                })
        except Exception:
            pass
    return skills


def _get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _locks:
        _locks[chat_id] = asyncio.Lock()
    return _locks[chat_id]


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _is_allowed_user(user_id: int) -> bool:
    """Fail-closed: empty allowlist = no access."""
    return user_id in config.telegram_allowed_users


# ---------------------------------------------------------------------------
# Gateway client helpers
# ---------------------------------------------------------------------------
async def _ensure_session(user_id: int, chat_id: int) -> str:
    """Get active session for this chat, or create one."""
    async with http.get(
        f"{SERVER_URL}/sessions",
        params={"client_type": "telegram", "client_ref": str(chat_id)},
    ) as resp:
        data = await resp.json()
        for s in data:
            if s.get("is_active"):
                return s["id"]

    async with http.post(
        f"{SERVER_URL}/sessions",
        json={
            "owner_type": "telegram",
            "owner_ref": str(user_id),
            "client_ref": str(chat_id),
        },
    ) as resp:
        return (await resp.json())["id"]


async def _query(prompt: str, user_id: int, chat_id: int) -> str:
    """Send message to gateway, stream SSE response, return text."""
    session_id = await _ensure_session(user_id, chat_id)
    result_text = ""
    assistant_texts: list[str] = []

    async with http.post(
        f"{SERVER_URL}/sessions/{session_id}/message",
        json={"content": prompt},
    ) as resp:
        async for line in resp.content:
            line = line.decode().strip()
            if not line or not line.startswith("data: "):
                continue
            try:
                event = json.loads(line.removeprefix("data: "))
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        assistant_texts.append(block["text"])
            elif etype == "result":
                result_text = event.get("result", "")

    return "\n\n".join(assistant_texts) or result_text or "(No response)"


async def _server_command(cmd: str, user_id: int, chat_id: int) -> dict:
    """Send a server command and return the JSON response."""
    async with http.post(
        f"{SERVER_URL}/command",
        json={
            "content": cmd,
            "owner_type": "telegram",
            "owner_ref": str(user_id),
            "client_ref": str(chat_id),
        },
    ) as resp:
        return await resp.json()


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
# Server command formatters
# ---------------------------------------------------------------------------
def _time_ago(ts: float) -> str:
    delta = datetime.now().timestamp() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta / 60)} min ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


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
        ago = _time_ago(last) if last else "?"
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
    result = await _server_command(content, user_id, chat_id)

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
    skills = _get_skills()
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
    lock = _get_lock(chat_id)
    async with lock:
        response = await _query(prompt, update.effective_user.id, chat_id)
        chunks = _chunk_message(response)
        await update.message.reply_text(chunks[0])
        for chunk in chunks[1:]:
            await update.effective_chat.send_message(chunk)


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
    lock = _get_lock(chat_id)

    if lock.locked():
        await update.message.reply_text("Still processing your previous message, please wait.")
        return

    async with lock:
        try:
            response = await _query(content, update.effective_user.id, chat_id)
            chunks = _chunk_message(response)
            await update.message.reply_text(chunks[0])
            for chunk in chunks[1:]:
                await update.effective_chat.send_message(chunk)
        except Exception:
            log.exception("Error processing message in chat %s", chat_id)
            await update.message.reply_text("Something went wrong. Try again or use /clear.")


# ---------------------------------------------------------------------------
# Voice message handler
# ---------------------------------------------------------------------------
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed_user(update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    lock = _get_lock(chat_id)

    if lock.locked():
        await update.message.reply_text("Still processing your previous message, please wait.")
        return

    async with lock:
        try:
            # Download OGG/Opus from Telegram
            voice_file = await update.message.voice.get_file()
            ogg_bytes = bytes(await voice_file.download_as_bytearray())

            # STT via voice-server
            text = await _stt(ogg_bytes)
            log.info("STT: %r", text[:80])

            if not text.strip():
                await update.message.reply_text("Couldn't transcribe audio.")
                return

            # Query gateway
            response = await _query(text, update.effective_user.id, chat_id)

            # TTS via voice-server
            ogg_response = await _tts(response)

            # Reply with voice note
            await update.message.reply_voice(voice=io.BytesIO(ogg_response))

        except Exception:
            log.exception("Error processing voice in chat %s", chat_id)
            await update.message.reply_text("Something went wrong with voice processing.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _get_bot_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if token:
        return token
    try:
        import keyring
        token = keyring.get_password("hive-mind", "TELEGRAM_BOT_TOKEN")
    except Exception:
        pass
    if not token:
        log.error("TELEGRAM_BOT_TOKEN not found. Set it in .env: TELEGRAM_BOT_TOKEN=your-token")
        sys.exit(1)
    return token


async def _on_startup(app) -> None:
    global http
    http = aiohttp.ClientSession()
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    app.run_polling()
