"""
Hive Mind Telegram Bot.

Thin HTTP client to the gateway server (server.py).
Supports text messages and voice notes (STT/TTS via voice-server).
All Claude Code interaction flows through the gateway — no SDK dependency.
"""

import asyncio
import contextlib
import functools
import io
import json
import logging
import os
import sys
import time

import aiohttp
from telegram import ForceReply, Update
from telegram.constants import ChatAction
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from hive_surfaces.config import config, photo_root
from hive_surfaces.bot_utils import claim_picker, get_lock, get_queue, time_ago
from hive_surfaces.gateway_client import GatewayClient
from hive_surfaces import hitl, model_picker, rename_prompt, session_picker, voice_routing
from hive_surfaces.skills import get_skills
from hive_surfaces.hive_logging import configure_logging, log_event

log = configure_logging("hive-mind-telegram")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TELEGRAM_MSG_LIMIT = 4096
# Gateway/sessions/broker → hive-comms (containerised, NS-owned).
COMMS_URL = os.environ.get("COMMS_URL", "http://127.0.0.1:8426")
COMMS_BEARER_TOKEN = os.environ.get("COMMS_BEARER_TOKEN", "")
# The one credential that may change a conversation's name, held apart from the
# service bearer. It is the same secret the browser terminal used to check when
# it owned the name store, kept small on purpose: a copy escaping into a log or
# a screenshot is worth exactly one renamed conversation.
TERMINAL_LABELS_TOKEN = os.environ.get("TERMINAL_LABELS_TOKEN", "")
VOICE_SERVER_URL = os.environ.get("VOICE_SERVER_URL", "http://localhost:8422")
# A host running both engines names each one's server; a host running one
# names neither and `VOICE_SERVER_URL` answers for whichever engine its minds
# picked. Which of the two a given mind is spoken by comes off that mind's own
# record, never off this environment.
VOICE_SERVER_URL_CHATTERBOX = os.environ.get("VOICE_SERVER_URL_CHATTERBOX", "")
VOICE_SERVER_URL_KOKORO = os.environ.get("VOICE_SERVER_URL_KOKORO", "")
# When on (default), every conversational reply is voiced: text still streams,
# a voice note follows. Set ALWAYS_VOICE=0 in .env for text-only except when
# the user sends a voice message. A missing/unreachable voice server degrades
# to text-only (the TTS task logs and drops).
ALWAYS_VOICE = os.environ.get("ALWAYS_VOICE", "1").strip().lower() not in ("0", "false", "no", "")
# Hive-tools — used by /models to list Ollama-served models.
HIVE_TOOLS_URL = os.environ.get("HIVE_TOOLS_URL", "http://127.0.0.1:9421")
HIVE_TOOLS_TOKEN = os.environ.get("HIVE_TOOLS_TOKEN", "")

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
# The background slash-menu publisher, kept referenced for its lifetime.
_menu_task: "asyncio.Task | None" = None


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _is_allowed_user(user_id: int) -> bool:
    """Fail-closed: empty allowlist = no access."""
    return user_id in config.telegram_allowed_users


# ---------------------------------------------------------------------------
# Voice helpers
# ---------------------------------------------------------------------------
#: Resolves a mind to the voice server that speaks for it. Built once: the
#: listing behind it is cached with its own TTL, so a mind's engine can change
#: while this process runs.
_voice_servers = voice_routing.VoiceServerResolver(
    COMMS_URL,
    COMMS_BEARER_TOKEN or "",
    {
        voice_routing.CHATTERBOX: VOICE_SERVER_URL_CHATTERBOX,
        voice_routing.KOKORO: VOICE_SERVER_URL_KOKORO,
    },
    fallback_url=VOICE_SERVER_URL,
)


def _voice_server() -> str:
    """The voice server this mind's own record points at."""
    return _voice_servers.resolve(_voice_id())


async def _stt(ogg_bytes: bytes) -> str:
    """POST OGG audio to voice-server /stt, return transcribed text.

    Sent to the same server that speaks for this mind. Both engines ship the
    same faster-whisper half, and a host that configured only the engine it
    uses has no other server to ask.
    """
    form = aiohttp.FormData()
    form.add_field("file", ogg_bytes, filename="audio.ogg", content_type="audio/ogg")
    async with http.post(f"{_voice_server()}/stt", data=form) as resp:
        if resp.status != 200:
            raise RuntimeError(f"STT error {resp.status}: {await resp.text()}")
        return (await resp.json())["text"]


def _voice_id() -> str:
    """Which mind the voice server is being asked to speak as.

    The gateway client's `mind_id` first, which is the same identifier the
    Discord surface sends and is always present in a process that has a
    gateway. `MIND_NAME` was read alone before, and the containerised stack
    runs each surface in its own container holding `MIND_ID` but no
    `MIND_NAME` — so every reply asked for the voice of a mind called
    "default", the voice server resolved nobody, and each mind spoke in the
    server's fallback voice however carefully its own had been chosen.
    """
    if gateway is not None and gateway.mind_id:
        return str(gateway.mind_id)
    return os.getenv("MIND_NAME") or os.getenv("MIND_ID") or "default"


async def _tts(text: str) -> bytes:
    """POST text to voice-server /tts, return OGG audio bytes."""
    async with http.post(f"{_voice_server()}/tts", json={"text": text, "voice_id": _voice_id()}) as resp:
        if resp.status != 200:
            raise RuntimeError(f"TTS error {resp.status}: {await resp.text()}")
        return await resp.read()


# ---------------------------------------------------------------------------
# JSON detection / sanitization helpers
# ---------------------------------------------------------------------------
def _looks_like_json(text: str) -> bool:
    """Return True if text looks like a raw JSON object or array."""
    stripped = text.strip()
    if not stripped:
        return False
    if not (stripped.startswith("{") or stripped.startswith("[")):
        return False
    try:
        parsed = json.loads(stripped)
        # Only consider dicts and lists as "JSON payloads" — not bare
        # strings, numbers, booleans, or null.
        return isinstance(parsed, (dict, list))
    except (json.JSONDecodeError, ValueError):
        return False


def _sanitize_response(text: str) -> str:
    """Replace raw JSON payloads with a human-readable confirmation."""
    if _looks_like_json(text):
        return "Done."
    return text


# ---------------------------------------------------------------------------
# Message chunking (Telegram's 4096-char limit)
# ---------------------------------------------------------------------------
def _utf16_len(text: str) -> int:
    """Length as Telegram counts it: UTF-16 code units, not code points.

    An emoji outside the BMP is one Python character and two of these. A chunk
    of 4096 characters carrying any of them is over the limit, and Telegram
    rejects it — which used to be one loud `MESSAGE_TOO_LONG` and is now,
    behind a retrying delivery path, a minute of retrying a rejection that can
    never succeed.
    """
    return len(text.encode("utf-16-le")) // 2


def _chunk_message(text: str) -> list[str]:
    """Split text into pieces Telegram will accept.

    Sized in UTF-16 code units. The fast path is the common one — text that is
    plainly short enough measured the cheap way needs no encoding at all.
    """
    if len(text) <= TELEGRAM_MSG_LIMIT and _utf16_len(text) <= TELEGRAM_MSG_LIMIT:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    width = 0
    for ch in text:
        ch_width = 2 if ord(ch) > 0xFFFF else 1
        if width + ch_width > TELEGRAM_MSG_LIMIT:
            chunks.append("".join(current))
            current, width = [], 0
        current.append(ch)
        width += ch_width
    if current:
        chunks.append("".join(current))
    return chunks or [""]


# ---------------------------------------------------------------------------
# Guaranteed delivery
# ---------------------------------------------------------------------------
# How hard to try before handing the text to the proactive queue. Three
# attempts over ~3s covers the shape of failure actually seen on this host —
# a burst of `Bad Gateway` from api.telegram.org lasting seconds — without
# holding the handler open long enough to matter.
_DELIVER_ATTEMPTS = 3
_DELIVER_BACKOFF_S = 1.0
# How long the proactive consumer keeps trying one message, and how long it
# waits between turns at the queue.
#
# The outage this exists to survive lasted *hours* — api.telegram.org 502ing
# while taps piled up at Telegram's end. A retry budget measured in seconds
# does not meet that; it just relocates where the answer is lost. Sixty
# attempts three minutes apart is three hours of trying.
#
# Bounded all the same, because a message with nowhere to go — the bot
# blocked, the chat deleted — retried forever is a queue that never empties.
# When the budget runs out the text goes to the journal, which is a worse
# place than the operator's phone but a far better one than nowhere.
_PROACTIVE_MAX_ATTEMPTS = 60
_PROACTIVE_RETRY_S = 180.0


async def _deliver(bot, chat_id: int, text: str) -> bool:
    """Put ``text`` in front of the operator, and never silently fail to.

    A tapped button that produces no visible answer is indistinguishable from
    a bot that has died, and the operator's rational response — tap it again —
    is what destroyed a conversation on 2026-09-17: two `New session` taps 75
    seconds apart, each one ending what the last had created, because neither
    reply arrived to say the first had worked.

    The send used to be a bare `reply_text` outside any try, so a `NetworkError`
    escaped into the global error handler, which logs transient network errors
    at INFO and returns. The answer was gone, at a log level nobody reads,
    while the action it described had already happened.

    So: retried, then queued. The proactive queue is the same path unsolicited
    turns already take, and it drains whenever the connection comes back — the
    answer arrives late rather than never. Returns whether it went out on this
    call; a queued message reports ``False`` because it has not been delivered
    yet.
    """
    chunks = _chunk_message(text)
    sent = 0
    last: Exception | None = None
    for attempt in range(_DELIVER_ATTEMPTS):
        try:
            # Resumed from where the last attempt stopped. Restarting the loop
            # re-sends chunks that already landed, so a three-part answer
            # failing on part two arrives as 1, 2, 1, 2, 3 — and reports
            # success.
            while sent < len(chunks):
                await bot.send_message(chat_id=chat_id, text=chunks[sent])
                sent += 1
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt + 1 < _DELIVER_ATTEMPTS:
                await asyncio.sleep(_DELIVER_BACKOFF_S * (attempt + 1))
    # WARNING, not INFO. The distinction is the whole point: this names an
    # action that already happened whose answer the operator never saw, which
    # is the state the whole feature exists to make impossible to reach
    # quietly.
    log_event(
        log, "surface.delivery.failed", level=logging.WARNING, surface="telegram",
        client_ref=chat_id, error=str(last), content_chars=len(text),
    )
    from hive_surfaces import proactive

    # Only what has not landed. Queuing the whole text after a partial send
    # would repeat the part the operator already has.
    proactive.enqueue(chat_id, "".join(chunks[sent:]))
    return False


# ---------------------------------------------------------------------------
# Typing indicator
# ---------------------------------------------------------------------------
# Telegram's typing action expires after about five seconds, so a turn that
# runs for minutes needs it re-sent rather than set once. The refresh is a
# background task so it keeps firing underneath the streaming edits instead of
# stopping the moment text appears.
TYPING_REFRESH_S = 4.0


async def _send_typing(bot, chat_id: int) -> None:
    """One typing action, which never fails the turn it decorates."""
    try:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
    except Exception:
        pass  # a dropped action is cosmetic; never fail the turn for it


@contextlib.asynccontextmanager
async def _typing(bot, chat_id: int):
    """Show "typing" in chat_id for as long as the block runs.

    The first action is sent inline, before the block is entered, rather than
    left to the refresh task: a task only runs once the loop next idles, so a
    handler that reaches its first await a second in would show nothing for
    that second — and the whole point of holding the indicator from the
    message's arrival is that there is no gap at the front.
    """
    await _send_typing(bot, chat_id)

    async def _loop() -> None:
        while True:
            await asyncio.sleep(TYPING_REFRESH_S)
            await _send_typing(bot, chat_id)

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task



def with_typing(handler):
    """Hold the typing indicator for as long as `handler` runs.

    Applied at registration rather than inside the streaming helper, because
    the streaming helper is not where most of the waiting happens: a voice
    note is downloaded and transcribed before a single token exists, a photo
    is fetched at full resolution, and every slash command is a gateway round
    trip of its own. Wrapping one of those and not the others is how the
    indicator comes to mean "the mind is answering in prose" instead of "the
    mind is working", which is the only thing the user is reading it for.
    """

    @functools.wraps(handler)
    async def _wrapped(update, context):
        chat = update.effective_chat
        if chat is None:
            return await handler(update, context)
        async with _typing(update.get_bot(), chat.id):
            return await handler(update, context)

    return _wrapped


async def _reply_chunked(update: Update, text: str) -> None:
    """Reply with text of any length, and never silently fail to.

    Command replies used to go straight to ``reply_text``, so one that ran past
    Telegram's limit came back as a BadRequest the error handler logs as a
    transient network blip and drops. The user sees nothing at all — the
    command reads as broken rather than as too chatty. A `NetworkError` does
    the same thing, which is why this goes through `_deliver`: a typed command
    deserves the guarantee a tapped button got, and `/sessions` failing
    silently during an outage is how the operator ends up with no picker and no
    idea why.
    """
    await _deliver(update.get_bot(), update.effective_chat.id, text)


# ---------------------------------------------------------------------------
# Streaming helper
# ---------------------------------------------------------------------------
async def _stream_to_message(
    sent,
    user_id: int,
    chat_id: int,
    prompt: str,
    edit_interval: float = 2.0,
    images: list[dict] | None = None,
    voice: bool = False,
    chat=None,
) -> list[str]:
    """Stream a gateway response, progressively editing sent as chunks arrive.

    Returns the final list of message chunks.

    When voice=True, the full response is converted to a single voice message
    after streaming completes, so text arrives progressively and voice follows.
    """
    accumulated = ""
    last_edit = 0.0
    # The messages this reply occupies, in order, the first being the
    # placeholder we were handed. A long answer outgrows one: Telegram caps a
    # message, so previewing chunk zero forever means the text visibly stops
    # moving partway through — identical on screen to a mind that wedged, which
    # is the one thing a progress display must never look like.
    bubbles = [sent]

    # The indicator is held by `with_typing` around the whole handler, which
    # started before this stream and outlasts it.
    async for text_chunk in gateway.query_stream(user_id, chat_id, prompt, images=images):
        # Concatenate without separator. Per-token deltas (when the mind has
        # --include-partial-messages enabled) include their own whitespace;
        # buffered assistant text already has its own paragraph breaks.
        accumulated += text_chunk
        now = time.monotonic()
        if now - last_edit >= edit_interval:
            pieces = _chunk_message(accumulated)
            try:
                # Earlier bubbles are finished text and are written once, so a
                # rolled-over answer does not re-edit the whole reply each tick.
                for index in range(len(bubbles) - 1, len(pieces) - 1):
                    await bubbles[index].edit_text(pieces[index])
                while len(bubbles) < len(pieces):
                    bubbles.append(
                        await bubbles[-1].reply_text(pieces[len(bubbles)])
                    )
                await bubbles[-1].edit_text(pieces[-1])
            except Exception:
                pass  # MessageNotModified or rate limit — skip this update
            last_edit = now

    if not accumulated:
        # The gateway forwards mind errors as a result event, which arrives
        # via query_stream's result fallback — so an empty stream here means
        # nothing was reported at all.
        accumulated = (
            "ERROR: mind stream ended with no text and no error event. "
            "Check the mind service logs and the hive-comms logs."
        )

    final_chunks = [_sanitize_response(c) for c in _chunk_message(accumulated)]
    # Every bubble gets its sanitized final text, and any the stream never
    # reached is sent now. Each edit is attempted independently: one message
    # refused for being unchanged must not cost the rest their final copy.
    for index, piece in enumerate(final_chunks):
        try:
            if index < len(bubbles):
                await bubbles[index].edit_text(piece)
            else:
                bubbles.append(await bubbles[-1].reply_text(piece))
        except Exception:
            log.debug("Final edit of bubble %s failed", index, exc_info=True)

    # Send one voice message with the complete response — detached so it
    # doesn't hold the chat lock while TTS round-trips. The text response is
    # already shown above; the voice arrives whenever the TTS service is
    # done. Without this, holding the lock through TTS makes follow-up
    # messages queue up and creates the "response held until next message"
    # n+1 sync glitch.
    if voice and chat:
        full_text = accumulated.strip()
        if full_text:
            async def _send_voice_bg() -> None:
                try:
                    ogg = await _tts(full_text)
                    await chat.send_voice(voice=io.BytesIO(ogg))
                except Exception:
                    log.warning("Final voice TTS/send failed", exc_info=True)
            asyncio.create_task(_send_voice_bg())

    return final_chunks


# ---------------------------------------------------------------------------
# Server command formatters
# ---------------------------------------------------------------------------
def _format_queue_batch(messages: list[str]) -> str:
    """Combine queued messages into one prompt so Claude replies once."""
    if len(messages) == 1:
        return messages[0]
    items = "\n".join(f"{i + 1}. {m}" for i, m in enumerate(messages))
    return (
        "While you were processing my previous message, I sent several more. "
        "Please address all of them in one reply:\n\n" + items
    )


def format_model_result(result: object) -> str:
    """What a `/model` answer says to the operator.

    A listing is not a switch. The gateway answers a bare `/model` with
    ``{"models": [...]}``, and this used to test the answer for being a
    *list* — which a dict is not — so the listing fell straight through to the
    switch report and read "Switched to None": a switch nothing had performed,
    named after a field the answer does not carry. A bare `/model` now draws
    the picker and never arrives here; a listing that arrives anyway is still
    not reported as a switch, and an answer naming no model claims nothing.
    """
    rows = model_picker.models_from(result)
    if rows:
        lines = ["Available models:"]
        for row in rows:
            provider = row.get("provider_label") or row.get("provider")
            lines.append(f"- {row['name']}" + (f" ({provider})" if provider else ""))
        lines.append("\nSend /model to pick one.")
        return "\n".join(lines)
    switched = result.get("model") if isinstance(result, dict) else None
    if not switched:
        return "No model change was made \u2014 send /model to pick one."
    msg = f"Switched to {switched}"
    if isinstance(result, dict) and result.get("warning"):
        msg += f"\n\u26a0\ufe0f {result['warning']}"
    return msg


def _format_status(data: dict) -> str:
    return (
        f"Server port: {data.get('server_port')}\n"
        f"Default model: {data.get('default_model')}\n"
        f"Sessions: {data.get('running_sessions')}/{data.get('total_sessions')} running"
    )


# ---------------------------------------------------------------------------
# Server command dispatcher
# ---------------------------------------------------------------------------
SERVER_COMMANDS = {"/clear", "/model", "/autopilot", "/kill", "/prune", "/status", "/sessions", "/switch", "/new", "/remember"}

# Shown on the button itself the moment a tap is received, before any work
# runs. Telegram renders it as a toast over the chat, which is the only
# feedback available that costs no round trip of its own — answering the
# callback query is mandatory anyway.
ACK_TEXT = "Working…"

# What a tap on an already-used picker says. It exists for the case where the
# keyboard removal failed and the buttons are still sitting there: the tap is
# refused, but it is refused out loud.
SPENT_PICKER_TEXT = (
    "That list has already been used — send /sessions for a fresh one."
)


def _conversation_caption(result: dict) -> str:
    """Name a conversation the way the picker named it.

    One function for the button and for the reply that follows tapping it, so a
    row reading "dragoman" cannot answer "Resumed New session". The name rides
    on the session row the gateway just returned — it used to be fetched from a
    second store over HTTP, which meant the reply and the button could disagree
    whenever that store was unreachable.
    """
    return session_picker.caption_for(result)


async def _handle_server_command(content: str, user_id: int, chat_id: int) -> str:
    parts = content.split()
    cmd = parts[0]
    log_event(
        log, "surface.command.received", surface="telegram", command=cmd,
        user_id=user_id, client_ref=chat_id,
    )
    result = await gateway.server_command(user_id, chat_id, content)

    # `server_command` returns the parsed body whatever the status was, and
    # FastAPI reports its own rejections as `detail`, not `error` — a 401 on a
    # rotated bearer, a 404 on a swept session, a 422 on a body it will not
    # parse. Reading only `error` let all three fall through to the success
    # branch below, so a switch that never happened answered `Resumed "..."`
    # and the operator's next message went to the conversation they were
    # already in. `_suspend_conversation` learned this from a 422 and fixed it
    # in one place; this is the other.
    problem = None
    if isinstance(result, dict):
        problem = result.get("error") or result.get("detail")
    if problem:
        log_event(
            log, "surface.command.failed", level=logging.WARNING, surface="telegram",
            command=cmd, user_id=user_id, client_ref=chat_id,
        )
        return f"Error: {problem}"
    log_event(
        log, "surface.command.completed", surface="telegram", command=cmd,
        user_id=user_id, client_ref=chat_id,
    )

    if cmd == "/status":
        return _format_status(result)
    if cmd == "/new":
        return f"New session: {result.get('id', '?')[:8]}"
    if cmd == "/clear":
        return f"Session cleared. New: {result.get('id', '?')[:8]}"
    if cmd == "/model":
        return format_model_result(result)
    if cmd == "/autopilot":
        on = result.get("autopilot", False)
        summary = _conversation_caption(result)
        if on:
            return f"\U0001f916 Autopilot ON for \"{summary}\""
        return f"\U0001f512 Autopilot OFF for \"{summary}\""
    if cmd == "/switch":
        return f"Resumed \"{_conversation_caption(result)}\""
    if cmd == "/kill":
        caption = _conversation_caption(result)
        return f"Killed \"{caption}\" (status: {result.get('status')})"
    if cmd == "/prune":
        killed = result.get("killed") or []
        kept = result.get("kept")
        if not killed:
            return "Nothing to prune — only the active session exists."
        kept_str = f" Kept active: {kept[:8]}." if kept else ""
        return f"Pruned {len(killed)} session(s).{kept_str}"

    return "Done."


# ---------------------------------------------------------------------------
# Auth guard helper
# ---------------------------------------------------------------------------
async def _auth_check(update: Update) -> bool:
    if not _is_allowed_user(update.effective_user.id):
        log_event(
            log, "surface.auth.rejected", level=logging.WARNING, surface="telegram",
            user_id=update.effective_user.id, client_ref=update.effective_chat.id,
        )
        await update.message.reply_text("Not authorized.")
        return False
    return True


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The picker: one tappable button per conversation.

    The old numbered list made the reader resolve a position into a
    conversation, which only works if nothing has moved since it was printed.
    On a phone the message sits in scrollback for days. Buttons carry the id.
    """
    if not await _auth_check(update):
        return
    await _send_session_picker(
        context.bot, update.effective_user.id, update.effective_chat.id
    )


async def _send_session_picker(bot, user_id: int, chat_id: int) -> None:
    """Draw a conversation picker into the chat, from the list as it is now.

    Shared by `/sessions` and by the failure path of a tap. A picker is spent
    by its one action and is never un-spent, so the way a failed tap leaves
    the operator with a working list is that it hands them a *new* one —
    drawn against current state, and therefore not offering the conversation
    that just refused them.

    Resurrecting the old picker instead was tried and is worse in three
    separate ways: un-spending a claim erases the row from
    `data/spent_pickers.json`, which is the only thing standing between a
    tap Telegram redelivers after a restart and a second action; a `/new`
    whose response was lost has already killed the conversation comms kills
    *before* it creates the replacement, so "the tap failed" does not mean
    "nothing happened"; and a keyboard restored into scrollback stays
    tappable for months, where one thumb on a stale row kills whatever
    browser terminal now holds that id.
    """
    result = await gateway.server_command(user_id, chat_id, "/sessions")
    if isinstance(result, dict):
        # FastAPI reports its own rejections as `detail`, not `error` — a 401
        # on a rotated bearer, a 422 on a body it will not parse. Reading only
        # `error` let those fall through as "not a list", so `visible_sessions`
        # was handed `[]` and an expired token was reported to the operator as
        # "No live conversations." — whose reasonable next move is to tap New
        # session. `_handle_server_command` and `_suspend_conversation` each
        # learned this separately; this is the third site.
        problem = result.get("error") or result.get("detail")
        await _deliver(bot, chat_id, f"Error: {problem}" if problem else
                       "Couldn't read the conversation list.")
        return
    # Filtered before anything is counted, so the "newest N of M" the operator
    # reads is about the list they can actually see.
    sessions = session_picker.visible_sessions(result if isinstance(result, list) else [])
    shown = min(len(sessions), session_picker.MAX_PICKER_ROWS)
    if not sessions:
        header = "No live conversations."
    elif len(sessions) > shown:
        header = f"Your conversations \u2014 newest {shown} of {len(sessions)}:"
    else:
        header = "Your conversations:"
    # Retried, because this is the message that *recovers* from a failed tap.
    # A bare send here fails into `_on_error`, which logs a network error at
    # INFO and returns — so during an outage the picker never appears, says
    # nothing, and leaves nothing above INFO to find.
    keyboard = session_picker.build_session_keyboard(sessions)
    last: Exception | None = None
    for attempt in range(_DELIVER_ATTEMPTS):
        try:
            await bot.send_message(chat_id=chat_id, text=header, reply_markup=keyboard)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt + 1 < _DELIVER_ATTEMPTS:
                await asyncio.sleep(_DELIVER_BACKOFF_S * (attempt + 1))
    log_event(
        log, "surface.picker.send.failed", level=logging.WARNING, surface="telegram",
        user_id=user_id, client_ref=chat_id, error=str(last),
    )
    # A picker cannot be queued — its buttons are only meaningful against the
    # list as it was — so what gets queued is the fact that it failed.
    await _deliver(
        bot, chat_id,
        "Couldn't draw the conversation list \u2014 send /sessions to try again.",
    )


async def on_session_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A tapped button. Every path answers the callback before it returns.

    Telegram spins a progress ring on the button until the query is answered,
    and an unanswered one never times out visibly — it just stays spinning,
    which reads as the bot having died rather than as anything going wrong.
    So the answer comes first and the work happens after it.
    """
    query = update.callback_query
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if not _is_allowed_user(user_id):
        # Every typed command logs its rejection; a tap is the one surface
        # where an unauthorised attempt used to leave no record at all.
        log_event(
            log, "surface.auth.rejected", level=logging.WARNING, surface="telegram",
            user_id=user_id, client_ref=chat_id,
        )
        # Bare, this raises on a redelivered tap whose query id has expired,
        # and the raise is the whole handler. Nothing else is said to an
        # unknown caller on purpose: telling them what happened is a gift to
        # whoever is probing the bot.
        try:
            await query.answer("Not authorized.", show_alert=True)
        except Exception:  # noqa: BLE001
            pass
        return
    # Acknowledged before any work starts, so the tap is visibly received even
    # when the answer behind it is still in flight or never arrives. A tap
    # redelivered across a restart can be too old to answer, and the 400 that
    # comes back used to abort the handler before the action ever ran — the
    # spinner is cosmetic, the action is not.
    acknowledged = True
    try:
        await query.answer(ACK_TEXT)
    except Exception as exc:  # noqa: BLE001
        acknowledged = False
        log_event(
            log, "surface.button.answer.failed", level=logging.WARNING,
            surface="telegram", user_id=user_id, client_ref=chat_id, error=str(exc),
        )
    if not acknowledged:
        # The toast is the cheap acknowledgement, and it is exactly the one
        # that fails in the case this was written for: a tap queued at
        # Telegram's end for hours arrives with an expired query id. Logging a
        # warning and carrying on leaves the operator with nothing on screen
        # in the one scenario where they most need something, so the chat
        # carries the acknowledgement instead.
        await _deliver(context.bot, chat_id, ACK_TEXT)

    action, target = session_picker.decode(query.data or "")
    log_event(
        log, "surface.button.tapped", surface="telegram", command=action,
        user_id=user_id, client_ref=chat_id,
    )

    # A picker is single use, and the claim is taken before anything is
    # awaited. Telegram delivered two `New session` taps 75 seconds apart on
    # 2026-09-17 — queued at its end while polling was down, then acted on
    # back to back — and the second one ended the conversation the first had
    # just created. The list a tap came from is stale the instant it is used;
    # what protects the operator is that it stops working, not that they
    # remember it is stale.
    #
    # A tap carrying no identifiable picker is refused rather than run. The
    # claim is the only thing standing between a stale tap and a destroyed
    # conversation, so a tap it cannot cover must not be the one that slips
    # past it.
    picker_id = getattr(query.message, "message_id", None)
    if picker_id is None or not claim_picker(chat_id, picker_id):
        log_event(
            log, "surface.button.spent", surface="telegram", command=action,
            user_id=user_id, client_ref=chat_id,
        )
        await _deliver(context.bot, chat_id, SPENT_PICKER_TEXT)
        return

    # The keyboard goes before the work, not after. It is the enforcement the
    # operator can see — a message with no buttons produces no taps — and
    # doing it first means a slow or failing action cannot leave a live
    # picker sitting there inviting a second tap. Best effort: the claim above
    # is what makes the guarantee, this is what makes it visible.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as exc:  # noqa: BLE001
        log_event(
            log, "surface.button.keyboard.clear.failed", level=logging.WARNING,
            surface="telegram", user_id=user_id, client_ref=chat_id, error=str(exc),
        )

    # Serialised against this chat's other work. Two taps on one row raced
    # each other into `activate_session`, which takes no lock of its own, and
    # landed two harness processes on a single transcript; a tap during a
    # streaming turn cut the answer off mid-sentence and it was presented as
    # complete. The button made both a one-finger operation.
    worked = True
    try:
        async with get_lock(chat_id):
            worked, msg = await _run_session_button(action, target, user_id, chat_id)
    except Exception as exc:  # noqa: BLE001
        worked = False
        # An exception escaping here leaves the tap looking like it did nothing
        # at all. The operator is told instead, and the traceback goes to the
        # log. Every path out of this handler ends in a delivered sentence.
        log_event(
            log, "surface.button.failed", level=logging.ERROR, surface="telegram",
            command=action, user_id=user_id, client_ref=chat_id, error=str(exc),
        )
        msg = "That didn't go through \u2014 the gateway didn't answer."

    # The picker itself records what it was used for. Without this the message
    # is a bare header over a vanished keyboard, and scrollback cannot say
    # which conversation a tap chose \u2014 which matters most in exactly the case
    # this feature is about, where the answer below arrived late or not at all.
    # The mark reflects the outcome. An unconditional tick left the picker
    # permanently reading "done: that didn't go through", which is a lie told
    # in scrollback long after the operator could check.
    mark = "\u2705" if worked else "\u26a0\ufe0f"
    header = getattr(query.message, "text", None) or "Your conversations:"
    try:
        await query.edit_message_text(f"{header}\n\n{mark} {msg}")
    except Exception as exc:  # noqa: BLE001
        log_event(
            log, "surface.button.caption.failed", level=logging.WARNING,
            surface="telegram", user_id=user_id, client_ref=chat_id, error=str(exc),
        )

    await _deliver(context.bot, chat_id, msg)

    if not worked:
        # The list this tap came from is spent for good — that is what stops a
        # stale tap acting, and it must survive a restart, so it is never
        # given back. What the operator gets instead is a current one, so a
        # refused conversation costs them a tap rather than the whole picker.
        #
        # Best effort, and last: the tap's own answer is already delivered
        # above, and a gateway too sick to draw a fresh list must not also
        # swallow the sentence explaining why the tap failed.
        try:
            await _send_session_picker(context.bot, user_id, chat_id)
        except Exception as exc:  # noqa: BLE001
            log_event(
                log, "surface.picker.redraw.failed", level=logging.WARNING,
                surface="telegram", user_id=user_id, client_ref=chat_id,
                error=str(exc),
            )


async def _suspend_conversation(target: str, user_id: int, chat_id: int) -> str:
    """Suspend one conversation and say what that meant for this chat.

    Shared because `/suspend` with an id and `/suspend` without one differ only
    in how the id was found; what suspending *costs* the operator is the same
    sentence either way and must not drift between two copies of it.
    """
    active = await gateway.find_active_session(user_id, chat_id)
    result = await gateway.suspend_session(target)
    # comms raises through its own handlers as {"error": ...}, but a body
    # FastAPI rejects comes back as {"detail": ...}. Reading only the first
    # reported a 422 as a successful suspend.
    if isinstance(result, dict):
        problem = result.get("error") or result.get("detail")
        if problem:
            return f"Error: {problem}"
    if target == active:
        # "It keeps its history" is true of the row and false of this chat:
        # suspending the conversation you are in clears the active binding,
        # so the next thing typed starts a fresh one with no history.
        return ("Suspended. That was the conversation this chat was in, so "
                "your next message starts a new one \u2014 tap it in /sessions "
                "to come back to it.")
    return "Suspended. It keeps its history \u2014 tap it in /sessions to resume."


async def _run_session_button(
    action: str, target: str, user_id: int, chat_id: int
) -> tuple[bool, str]:
    """What a tapped button actually does, and whether it worked.

    Returns ``(worked, message)``. The flag is returned rather than sniffed
    from the message, because there are three distinct failure shapes here — a
    raised exception, a gateway refusal reported in the string, and a tap that
    resolved to nothing — and only the first two look like failures from
    outside.
    """
    if action == session_picker.CB_NEW:
        # comms' /new closes the conversation this chat is holding before it
        # creates one, and closed is permanent — it never appears in a picker
        # again. One tap, no confirmation, so the reply has to say it.
        had_one = await gateway.find_active_session(user_id, chat_id)
        msg = await _handle_server_command("/new", user_id, chat_id)
        if msg.startswith("Error:"):
            # Appending on `had_one` alone told the operator their conversation
            # had been ended by a command that failed to end anything — so they
            # rebuild from scratch over a conversation that is still there.
            return False, msg
        if had_one:
            msg += "\nThe conversation you were in was ended."
        return True, msg
    if action == model_picker.CB_PICK and target:
        # The name travels whole, so the gateway resolves it against what the
        # mind offers now rather than against the keyboard's order. A switch
        # refused — a model withdrawn since the picker was drawn, or a turn
        # still streaming — is reported as the refusal it is.
        msg = await _handle_server_command(f"/model {target}", user_id, chat_id)
        return not msg.startswith("Error:"), msg
    if action == session_picker.CB_SWITCH and target:
        # The id travels whole, so the gateway resolves it against what exists
        # now rather than against the order this message was drawn in. A
        # conversation killed or swept away since then is reported as gone —
        # the one thing a tap must never do is land on a different one.
        msg = await _handle_server_command(f"/switch {target}", user_id, chat_id)
        return not msg.startswith("Error:"), msg
    # A tap that resolved to nothing is a failure, and saying so in the return
    # rather than in the prose is what keeps the mark on the picker honest: a
    # refusal that happens not to begin with "Error:" was being ticked.
    return False, "That button no longer means anything \u2014 send /sessions for a fresh list."


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Rename the conversation this chat is holding.

    A bare `/rename` asks for the name rather than printing a usage line.
    Tapping the command in Telegram's menu *sends* it — the menu does not type
    it into the composer for you to finish — so a usage line makes the menu
    entry useless to the one person it was added for: the operator on a phone
    who does not want to remember the syntax.

    An empty name is still never written: an empty name clears, so a rename
    that cannot name anything asks again instead of erasing what is there.
    """
    if not await _auth_check(update):
        return
    session_id, refusal = await _rename_target(
        update.effective_user.id, update.effective_chat.id
    )
    if refusal:
        await update.message.reply_text(refusal)
        return

    name = " ".join(context.args) if context.args else ""
    if not name.strip():
        # Asked, not refused. `ForceReply` opens the keyboard with the composer
        # already aimed at this message, so the next thing typed is the name —
        # which is the whole point of tapping a command rather than typing it.
        # `do_quote=True` makes the prompt a reply to the `/rename` that asked
        # for it, which is what gives `selective` a target. PTB does not quote
        # in a private chat by default, so without this the force-reply names
        # nobody: the Bot API targets "users @mentioned in the text" or, if the
        # bot's message is a reply, "the sender of the original", and the
        # prompt is neither.
        await update.message.reply_text(
            rename_prompt.PROMPT_TEXT,
            reply_markup=ForceReply(
                selective=True, input_field_placeholder="Conversation name"
            ),
            do_quote=True,
        )
        return

    await update.message.reply_text(await _apply_rename(session_id, name))


async def _rename_target(user_id: int, chat_id: int) -> tuple[str, str]:
    """The conversation a rename would land on, or why it cannot land at all.

    Returns ``(session_id, "")`` or ``("", refusal)``. Shared because a rename
    arriving as an argument and one arriving as a reply have to refuse for the
    same reasons in the same words — and because asking for a name before
    checking there is anything to name is how the operator gets prompted, types
    a name, and is only then told it was all for nothing.
    """
    # `ensure_session` creates when it finds nothing, and creating here mints a
    # conversation id, binds this chat to it and spawns a harness — so a rename
    # typed against a suspended conversation started an empty one and named
    # that instead, reporting success.
    try:
        session_id = await gateway.find_active_session(user_id, chat_id)
    except Exception as exc:  # noqa: BLE001
        # An unreachable gateway raises `aiohttp` errors, not telegram ones, so
        # this escaped the handler entirely and the operator got nothing at
        # all — neither the prompt nor a refusal.
        log_event(
            log, "surface.rename.lookup.failed", level=logging.WARNING,
            surface="telegram", client_ref=chat_id, error=str(exc),
        )
        return "", "Couldn't reach the gateway \u2014 nothing renamed. Try again."
    if not session_id:
        return "", (
            "This chat isn't in a conversation right now \u2014 send /sessions "
            "and tap one, or say anything to start one."
        )
    return session_id, ""


async def _apply_rename(session_id: str, name: str) -> str:
    """Write the name and say what happened, in one sentence either way.

    No read first. The write is partial, so a rename carries the name alone and
    the colour picked at the tile survives — which is what the read was for, and
    what made a rename fail outright whenever that read did.

    Each refusal says which one it was. "It didn't work" sends the operator to
    restart something; "that conversation has moved on" tells them to send
    /sessions and tap the live one, which is the only thing that helps.
    """
    body = session_picker.rename_body(name)
    if body is None:
        # A name that trimmed to nothing. Asking again beats a usage line for
        # the same reason the bare command asks in the first place.
        return rename_prompt.PROMPT_TEXT
    status = await gateway.rename_session(session_id, body)
    if status < 400 and status != 0:
        return f"Renamed to \"{body['name']}\"."
    if status == 409:
        return (
            "That conversation has already rotated away \u2014 nothing renamed. "
            "Send /sessions and tap the live one."
        )
    if status == 503:
        return "Renaming isn't wired up on this hive \u2014 nothing changed."
    if status == 401:
        # Named as a credential, not as a network fault. This mind may hold no
        # rename token at all, which is the state a fresh mind is installed in,
        # and "couldn't reach the gateway" sends the operator to restart a
        # container that is answering perfectly.
        return (
            "The gateway refused my rename credential \u2014 nothing changed. "
            "This mind needs TERMINAL_LABELS_TOKEN set to the hive's rename token."
        )
    return "Couldn't reach the gateway \u2014 name unchanged."


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    msg = await _handle_server_command("/new", update.effective_user.id, update.effective_chat.id)
    await _reply_chunked(update, msg)


async def cmd_remember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    msg = await _handle_server_command("/remember", update.effective_user.id, update.effective_chat.id)
    await _reply_chunked(update, msg)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    msg = await _handle_server_command("/clear", update.effective_user.id, update.effective_chat.id)
    await _reply_chunked(update, msg)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    msg = await _handle_server_command("/status", update.effective_user.id, update.effective_chat.id)
    await _reply_chunked(update, msg)


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Named, it switches. Bare, it offers the models as buttons.

    The names are deployment names rather than aliases, so asking the operator
    to retype one on a phone is asking for a typo — and a bare `/model` is the
    easiest thing in the world to send, because tapping the command in
    Telegram's menu sends it with no arguments at all.
    """
    if not await _auth_check(update):
        return
    name = " ".join(context.args).strip() if context.args else ""
    if not name:
        await _send_model_picker(
            context.bot, update.effective_user.id, update.effective_chat.id
        )
        return
    msg = await _handle_server_command(
        f"/model {name}", update.effective_user.id, update.effective_chat.id
    )
    await _reply_chunked(update, msg)


async def _send_model_picker(bot, user_id: int, chat_id: int) -> None:
    """Draw a model picker into the chat, from what the mind offers now.

    The list is the gateway's answer to a bare `/model`, which it gets by
    asking the mind, which asks its own inference proxy with its own key. So
    the buttons are the deployments this mind may actually address — a model
    it would be refused never appears, and nothing here holds a table that
    could disagree with the proxy.
    """
    result = await gateway.server_command(user_id, chat_id, "/model")
    if isinstance(result, dict) and not result.get("models"):
        # FastAPI reports its own rejections as `detail`, not `error`, and the
        # gateway refuses a bare `/model` with no active session — both have to
        # reach the operator as themselves rather than as "no models".
        problem = result.get("error") or result.get("detail")
        if problem:
            await _deliver(bot, chat_id, f"Error: {problem}")
            return
    keyboard = model_picker.build_model_keyboard(result, config.default_model)
    if keyboard is None:
        await _deliver(
            bot, chat_id,
            "No models offered \u2014 this mind cannot reach its provider right now.",
        )
        return
    await bot.send_message(
        chat_id=chat_id, text="Pick a model for this conversation:",
        reply_markup=keyboard,
    )


async def cmd_models(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Every model this mind may run, grouped by the provider hosting it.

    Asked of the inference proxy with this mind's own key, which is what
    decides the answer — a deployment this mind would be refused never
    appears. Nothing is assembled here and no alias is invented: a list that
    named models the proxy will not serve is a list that promises a switch it
    cannot make.
    """
    if not await _auth_check(update):
        return

    # The catalog belongs to the host: it is relayed from whatever inference
    # proxy this mind talks to, by a module a surface cannot declare as a
    # dependency. A mind that hands over no resolver offers no list, and says
    # so — the alternative was importing the host's module by name, which
    # resolved on exactly one machine and raised everywhere else, outside the
    # try below, so `/models` answered nothing at all.
    resolver = config.models_catalog
    if resolver is None:
        await update.message.reply_text(
            "This mind has no model list configured."
        )
        return

    try:
        rows = await resolver()
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(f"Could not read the model list: {exc}")
        return
    if not rows:
        await update.message.reply_text(
            "No models available — this mind cannot reach its provider right now."
        )
        return

    lines: list[str] = ["*Available models*"]
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row.get("provider_label") or row.get("provider") or "?", []).append(row)
    for provider_label in sorted(grouped):
        lines.append("")
        lines.append(f"*{provider_label}:*")
        for row in grouped[provider_label]:
            name = row["name"]
            label = row.get("label") or name
            marker = "  ← default" if name == config.default_model else ""
            suffix = f" — {label}" if label != name else ""
            lines.append(f"  • `{name}`{suffix}{marker}")

    lines.append("")
    lines.append("_Switch with_ `/model <name>`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_autopilot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    msg = await _handle_server_command("/autopilot", update.effective_user.id, update.effective_chat.id)
    await _reply_chunked(update, msg)


async def cmd_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    target = " ".join(context.args) if context.args else ""
    if not target:
        await update.message.reply_text("Usage: /switch <number>")
        return
    msg = await _handle_server_command(f"/switch {target}", update.effective_user.id, update.effective_chat.id)
    await _reply_chunked(update, msg)


async def cmd_suspend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Put a conversation to sleep. Bare means the one this chat is in.

    Given no id this is a verb about *here*, so it resolves the chat's own
    binding rather than asking the operator to copy an id out of a picker they
    are already inside of. An id is still accepted, because the picker draws
    one and a conversation held elsewhere has no other way to be reached.
    """
    if not await _auth_check(update):
        return
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    target = " ".join(context.args).strip() if context.args else ""
    if not target:
        target = await gateway.find_active_session(user_id, chat_id) or ""
        if not target:
            await update.message.reply_text(
                "This chat isn't in a conversation right now \u2014 send /sessions "
                "to see what there is, or /suspend with an id."
            )
            return
    # Serialised against this chat's other work: suspending under an in-flight
    # turn cuts the stream off mid-answer and the half of it that arrived is
    # presented as the whole reply.
    async with get_lock(chat_id):
        msg = await _suspend_conversation(target, user_id, chat_id)
    await _reply_chunked(update, msg)


async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    target = " ".join(context.args) if context.args else ""
    if not target:
        await update.message.reply_text("Usage: /kill <number>")
        return
    msg = await _handle_server_command(f"/kill {target}", update.effective_user.id, update.effective_chat.id)
    await _reply_chunked(update, msg)


async def cmd_prune(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    msg = await _handle_server_command("/prune", update.effective_user.id, update.effective_chat.id)
    await _reply_chunked(update, msg)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Interrupt the running command without killing the session.

    Bypasses the message queue entirely — does NOT acquire the chat lock.
    """
    if not await _auth_check(update):
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    session_id = await gateway.find_active_session(user_id, chat_id)

    if session_id is None:
        await update.message.reply_text("No active session.")
        return

    result = await gateway.interrupt_session(session_id)

    if result.get("message") == "nothing_running":
        await update.message.reply_text("Nothing running.")
    else:
        await update.message.reply_text("Interrupted.")


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
        final_chunks = await _stream_to_message(
            sent, update.effective_user.id, chat_id, prompt,
            voice=ALWAYS_VOICE, chat=update.effective_chat,
        )


# ---------------------------------------------------------------------------
# Text message handler
# ---------------------------------------------------------------------------
async def _handled_as_rename_reply(update, context, content: str) -> bool:
    """Take this message as a name if it answers the rename prompt.

    Shared by every inbound surface, because the prompt opens a reply box and
    what lands in it is whatever came to hand — typed, spoken, or a photo with
    a caption. A rename that works only for typed text is one that fails
    silently on the surface this mind is mostly used from.

    Recognised by what it replies *to*, plus that the replied-to message came
    from the bot. No pending-rename registry, no expiry, no per-chat state,
    which is what lets a prompt sent before a restart of the mind's service still be
    answered after one.
    """
    message = getattr(update, "message", None)
    if message is None:
        # PTB filters on `effective_message`, so an *edited* message re-fires
        # these handlers with `update.message` unset. Guarding inside the
        # helper is pointless while a caller dereferences it to build the
        # argument — the argument is evaluated first.
        return False
    replied = getattr(message, "reply_to_message", None)
    new_name = rename_prompt.name_from_reply(
        getattr(replied, "text", None),
        content,
        getattr(getattr(replied, "from_user", None), "is_bot", False),
    )
    if new_name is None:
        return False
    chat_id = update.effective_chat.id
    session_id, refusal = await _rename_target(update.effective_user.id, chat_id)
    # Through `_deliver`: a rename writes the label before it reports, so an
    # answer lost to a network blip is a mutation the operator never heard
    # about — the same defect the button path exists to close.
    await _deliver(context.bot, chat_id, refusal or await _apply_rename(session_id, new_name))
    return True


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed_user(update.effective_user.id):
        log_event(
            log, "surface.auth.rejected", level=logging.WARNING, surface="telegram",
            user_id=update.effective_user.id, client_ref=update.effective_chat.id,
        )
        return

    # Before the group gate, deliberately. `ForceReply(selective=True)` aims
    # the composer at the operator, so the natural thing to type in a group is
    # the bare name with no mention — and the gate below would drop it, giving
    # no rename, no turn and no reply at all.
    if await _handled_as_rename_reply(
        update, context, getattr(getattr(update, "message", None), "text", None) or ""
    ):
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

    log_event(
        log, "surface.message.received", surface="telegram", message_type="text",
        user_id=update.effective_user.id, client_ref=chat_id, content_chars=len(content),
    )
    lock = get_lock(chat_id)
    queue = get_queue(chat_id)

    if lock.locked():
        pos = queue.qsize() + 1
        await queue.put(content)
        await update.message.reply_text(f"Still processing — yours is queued (#{pos}).")
        return

    async with lock:
        try:
            sent = await update.message.reply_text("\u2026")
            final_chunks = await _stream_to_message(
                sent, update.effective_user.id, chat_id, content,
                voice=ALWAYS_VOICE, chat=update.effective_chat,
            )
        except Exception:
            log.exception("Error processing message in chat %s", chat_id)
            err = f"⚠ {type(_exc:=sys.exc_info()[1]).__name__}: {_exc}"[:3500]
            await update.message.reply_text(err)


        # Drain queue in a loop — new messages may arrive during batch processing
        while not queue.empty():
            queued: list[str] = []
            while not queue.empty():
                queued.append(queue.get_nowait())
            batch = _format_queue_batch(queued)
            try:
                sent2 = await update.effective_chat.send_message("\u2026")
                final_chunks2 = await _stream_to_message(
                    sent2, update.effective_user.id, chat_id, batch,
                    voice=ALWAYS_VOICE, chat=update.effective_chat,
                )
            except Exception:
                log.exception("Error processing queued batch in chat %s", chat_id)
                err = f"⚠ queued-batch {type(_exc:=sys.exc_info()[1]).__name__}: {_exc}"[:3500]
                await update.effective_chat.send_message(err)


# ---------------------------------------------------------------------------
# Photo message handler
# ---------------------------------------------------------------------------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed_user(update.effective_user.id):
        return

    # In group chats, only respond to @mentions in the caption
    if update.effective_chat.type != "private":
        bot_username = context.bot.username
        caption = update.message.caption or ""
        if f"@{bot_username}" not in caption:
            return
        caption = caption.replace(f"@{context.bot.username}", "").strip()
    else:
        caption = update.message.caption or ""

    content = caption if caption else "Please analyze this image."

    chat_id = update.effective_chat.id
    log_event(
        log, "surface.message.received", surface="telegram", message_type="photo",
        user_id=update.effective_user.id, client_ref=chat_id, caption_chars=len(content),
    )
    lock = get_lock(chat_id)

    if lock.locked():
        await update.message.reply_text("Still processing your previous message — yours is queued and will follow.")

    async with lock:
        try:
            import base64

            # Download highest resolution photo
            photo = update.message.photo[-1]
            file = await photo.get_file()
            photo_bytes = bytes(await file.download_as_bytearray())
            b64_data = base64.b64encode(photo_bytes).decode("ascii")

            # Persist to a dated drop folder so photos survive the turn. The
            # mind can move each file into the right house's images subfolder
            # via Bash. Without this, photos only existed as a base64 blob in
            # this turn's context and were lost forever after the turn closed.
            from datetime import datetime
            # The host's directory, never one derived from this file's
            # location: installed as a package, `parents[1]` is site-packages,
            # so a photo would land inside the virtualenv and the path handed
            # to the mind would name somewhere it has no business writing.
            root = photo_root()
            drop_dir = root / datetime.now().strftime("%Y-%m-%d")
            drop_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%H%M%S_%f")
            saved_path = drop_dir / f"{ts}_{chat_id}_{photo.file_unique_id}.jpg"
            saved_path.write_bytes(photo_bytes)
            relative = saved_path.relative_to(root.parent.parent)
            log_event(
                log, "surface.photo.saved", surface="telegram", user_id=update.effective_user.id,
                client_ref=chat_id, path=str(relative),
                bytes=len(photo_bytes),
            )

            images = [{"media_type": "image/jpeg", "data": b64_data}]

            # Tell the mind where the file lives, so a skill that files
            # images somewhere of its own can do it in the same turn. Where
            # that is belongs to the mind, not to the surface.
            content_with_path = f"{content}\n\n[image saved to {relative}]"

            sent = await update.message.reply_text("\u2026")
            final_chunks = await _stream_to_message(
                sent, update.effective_user.id, chat_id, content_with_path, images=images,
                voice=ALWAYS_VOICE, chat=update.effective_chat,
            )
        except Exception:
            log.exception("Error processing photo in chat %s", chat_id)
            await update.message.reply_text(
                f"⚠ image {type(_exc:=sys.exc_info()[1]).__name__}: {_exc}"[:3500]
            )



# ---------------------------------------------------------------------------
# Voice message handler
# ---------------------------------------------------------------------------
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed_user(update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    log_event(
        log, "surface.message.received", surface="telegram", message_type="voice",
        user_id=update.effective_user.id, client_ref=chat_id,
    )
    lock = get_lock(chat_id)
    queue = get_queue(chat_id)

    # STT happens outside the lock so we have text to queue if busy.
    # Telegram's getFile/download occasionally stalls; retry once on transient
    # network errors so a single blip doesn't surface to the user.
    async def _fetch_voice_bytes() -> bytes:
        voice_file = await update.message.voice.get_file()
        return bytes(await voice_file.download_as_bytearray())

    try:
        try:
            ogg_bytes = await _fetch_voice_bytes()
        except (TimedOut, NetworkError) as exc:
            log.warning("voice fetch transient failure (%s) — retrying once", exc.__class__.__name__)
            await asyncio.sleep(1.0)
            ogg_bytes = await _fetch_voice_bytes()
        text = await _stt(ogg_bytes)
    except Exception:
        log.exception("STT failed in chat %s", chat_id)
        await update.message.reply_text("Couldn't transcribe your audio.")
        return

    log.info("STT: %r", text[:80])
    if not text.strip():
        await update.message.reply_text("Couldn't transcribe audio.")
        return

    # The prompt opens a reply box, and on a surface that runs with voice on
    # the obvious way to answer it is to speak the name. Checked after STT
    # because that is the first point there is a name to read; without it the
    # spoken name went to the harness as a turn and the mind answered
    # "Dragoman?" while nothing was renamed.
    if await _handled_as_rename_reply(update, context, text):
        return

    if lock.locked():
        pos = queue.qsize() + 1
        await queue.put(text)
        await update.message.reply_text(f"Still processing — yours is queued (#{pos}).")
        return

    async with lock:
        try:
            sent = await update.message.reply_text("\u2026")
            final_chunks = await _stream_to_message(
                sent, update.effective_user.id, chat_id, text,
                voice=True, chat=update.effective_chat,
            )
        except Exception:
            log.exception("Unexpected error in voice handler for chat %s", chat_id)
            err = f"⚠ voice {type(_exc:=sys.exc_info()[1]).__name__}: {_exc}"[:3500]
            await update.message.reply_text(err)


        # Drain queue in a loop — new messages may arrive during batch processing
        while not queue.empty():
            queued: list[str] = []
            while not queue.empty():
                queued.append(queue.get_nowait())
            batch = _format_queue_batch(queued)
            try:
                sent2 = await update.effective_chat.send_message("\u2026")
                final_chunks2 = await _stream_to_message(
                    sent2, update.effective_user.id, chat_id, batch,
                    voice=ALWAYS_VOICE, chat=update.effective_chat,
                )
            except Exception:
                log.exception("Error processing queued batch in chat %s", chat_id)
                err = f"⚠ queued-batch {type(_exc:=sys.exc_info()[1]).__name__}: {_exc}"[:3500]
                await update.effective_chat.send_message(err)


# ---------------------------------------------------------------------------
# Catch-all for unregistered slash commands
# ---------------------------------------------------------------------------
async def handle_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route unregistered slash commands as prompts to the gateway.

    Any /command that is not handled by a registered CommandHandler falls
    through to this catch-all.  The full command text (including the /)
    is sent as a regular prompt so Claude can process it as a skill.
    """
    if not _is_allowed_user(update.effective_user.id):
        await update.message.reply_text("Not authorized.")
        return

    content = update.message.text or ""

    # Strip @botname suffix in group chats (e.g. /remember@botname → /remember)
    if context.bot.username:
        content = content.replace(f"@{context.bot.username}", "")
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
            sent = await update.message.reply_text("\u2026")
            final_chunks = await _stream_to_message(
                sent, update.effective_user.id, chat_id, content,
                voice=ALWAYS_VOICE, chat=update.effective_chat,
            )
        except Exception:
            log.exception("Error processing unknown command in chat %s", chat_id)
            err = f"⚠ {type(_exc:=sys.exc_info()[1]).__name__}: {_exc}"[:3500]
            await update.message.reply_text(err)


        # Drain queue in a loop — new messages may arrive during batch processing
        while not queue.empty():
            queued: list[str] = []
            while not queue.empty():
                queued.append(queue.get_nowait())
            batch = _format_queue_batch(queued)
            try:
                sent2 = await update.effective_chat.send_message("\u2026")
                final_chunks2 = await _stream_to_message(
                    sent2, update.effective_user.id, chat_id, batch,
                    voice=ALWAYS_VOICE, chat=update.effective_chat,
                )
            except Exception:
                log.exception("Error processing queued batch in chat %s", chat_id)
                err = f"⚠ queued-batch {type(_exc:=sys.exc_info()[1]).__name__}: {_exc}"[:3500]
                await update.effective_chat.send_message(err)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
# Re-exported from `token_store`, which owns them: the writer and this reader
# must agree on both the service and the key, and two declarations is how a
# token gets written under one spelling and read under another.
from hive_surfaces import token_store  # noqa: E402
from hive_surfaces.token_store import (  # noqa: E402
    DEFAULT_TOKEN_KEY,
    KEYRING_KEY_VAR,
    KEYRING_SERVICE,
)


def _get_bot_token() -> str:
    """Load this surface's Telegram bot token.

    Two deployments supply it two ways and both are right. An edge mind puts
    it in its own `.env`, one mind per host, so the environment is the answer.
    A stack runs several surfaces from one image on one machine, where the
    environment cannot hold several different values under one name — so each
    names its own `TELEGRAM_BOT_TOKEN_KEYRING_KEY` and the token lives in the
    keyring.

    A named key therefore wins over the environment, rather than the
    environment winning as it does when no key is named. The only reason to
    name one is that this surface's token is *not* the ambient one: a stack
    where the root `.env` happens to export some mind's token would otherwise
    start three bots polling Telegram as the same bot, each stealing the
    others' updates, with nothing in any log to say so.
    """
    token = token_store.resolve_token("telegram")
    if token:
        return token
    log.error(
        "No Telegram token in the environment or under %s.",
        token_store.names("telegram")[1],
    )
    sys.exit(1)


async def _on_startup(app) -> None:
    global http, gateway
    http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=0, sock_read=0))
    # Namespace the session client_type so two telegram bots with the same
    # authorized user don't share a session. MIND_ID is canonical — same
    # identity key used in broker.minds, sessions.mind_id, and lucent
    # writes. Don't drift back to MIND_NAME here; it creates a parallel
    # naming system for the same concept (see scripts/migrations/
    # 2026-05-15-surface-namespace-uuid.py for the cleanup that fixed it).
    mind_id = os.environ["MIND_ID"]
    surface_name = f"telegram:{mind_id}"
    gateway = GatewayClient(
        http, COMMS_URL, surface_name,
        surface_prompt=TELEGRAM_SURFACE_PROMPT,
        mind_id=mind_id,
        bearer_token=COMMS_BEARER_TOKEN or None,  # secret-guard: allow
        rename_token=TERMINAL_LABELS_TOKEN or None,  # secret-guard: allow
    )
    log.info(
        "Hive Mind Telegram bot started (gateway=%s, voice=%s)",
        COMMS_URL,
        VOICE_SERVER_URL,
    )
    log.info("Allowed users: %s", config.telegram_allowed_users)

    # Publish the slash menu, retrying in the background. Never fatal: a mind
    # whose menu could not be set is a mind you have to type commands at,
    # which is how it worked before this existed — not a reason to refuse to
    # start. But a single attempt at boot is published into whatever the
    # network happens to be doing at that second, and booting during the kind
    # of outage this branch exists to survive would leave the menu empty for
    # the life of the process.
    global _menu_task
    # Held on the module so the loop keeps a strong reference; a bare
    # `ensure_future` result can be collected mid-flight.
    _menu_task = asyncio.ensure_future(_publish_command_menu(app))


# Attempts at publishing the menu, and the wait between them. Generous, since
# nothing downstream depends on it and an empty menu is merely inconvenient.
_MENU_ATTEMPTS = 5
_MENU_RETRY_S = 60.0


async def _publish_command_menu(app) -> None:
    """Get the slash menu published, or say why it never was."""
    for attempt in range(_MENU_ATTEMPTS):
        try:
            await app.bot.set_my_commands(command_menu())
            log_event(
                log, "surface.command.menu.published", surface="telegram",
                content_chars=len(all_commands()),
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log_event(
                log, "surface.command.menu.failed", level=logging.WARNING,
                surface="telegram", error=str(exc), attempt=attempt + 1,
            )
            if attempt + 1 < _MENU_ATTEMPTS:
                await asyncio.sleep(_MENU_RETRY_S)


async def _on_shutdown(app) -> None:
    # Cancelled first: it sleeps a minute between attempts, and one waking
    # after the Application is gone calls `set_my_commands` on a dead bot and
    # goes on retrying into the shutdown.
    global _menu_task
    if _menu_task is not None:
        _menu_task.cancel()
        try:
            await _menu_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _menu_task = None
    # The queue is process memory. A restart with items pending loses them, and
    # losing them silently is the failure this whole path exists to close —
    # reached through the one action the operator takes by hand. Written out
    # before the process goes, so the answer survives somewhere.
    from hive_surfaces import proactive

    for chat_id, text, attempts in proactive.drain():
        log_event(
            log, "surface.proactive.undelivered_at_shutdown",
            level=logging.WARNING, surface="telegram", client_ref=chat_id,
            content_chars=len(text), attempt=attempts,
        )
        log.warning("undelivered text for chat %s: %s", chat_id, text)
    if http:
        await http.close()


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler. Swallow transient PTB network errors at info level;
    log everything else with traceback so the bot stays alive."""
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        log.info("transient telegram network error (%s): %s", err.__class__.__name__, err)
        return
    log.exception("unhandled exception in telegram handler", exc_info=err)


# ---------------------------------------------------------------------------
# The command table
# ---------------------------------------------------------------------------
# One list, feeding both the handlers and Telegram's slash menu. They were
# never going to stay in step as two lists: the bot had seventeen registered
# handlers and had published none of them, so `getMyCommands` returned an
# empty array and the menu offered nothing to tap.
#
# Descriptions are written for someone holding a phone who does not remember
# the syntax. A command taking an argument says so in the wording, because
# tapping a menu entry *sends* it — there is no half-typed command left in the
# composer to complete. `/rename` is the exception that proves it: it asks for
# the name instead, since it is the one where the argument is the whole point.
COMMANDS: tuple[tuple[str, str, object], ...] = (
    ("sessions", "List your conversations and switch between them", cmd_sessions),
    ("rename", "Name this conversation", cmd_rename),
    ("new", "Start a fresh conversation (ends this one)", cmd_new),
    ("clear", "Clear this conversation and start over", cmd_clear),
    ("status", "Server port, default model, sessions running", cmd_status),
    ("model", "Pick a model for this conversation", cmd_model),
    ("models", "List the models this mind can be pointed at", cmd_models),
    ("autopilot", "Toggle autopilot for this conversation", cmd_autopilot),
    ("switch", "Resume a conversation: /switch <id>", cmd_switch),
    ("suspend", "Put a conversation to sleep, keeping its history", cmd_suspend),
    ("kill", "End a conversation for good: /kill <id>", cmd_kill),
    ("prune", "End every conversation except the active one", cmd_prune),
    ("remember", "Save something to memory: /remember <what>", cmd_remember),
    ("stop", "Interrupt whatever the mind is working on", cmd_stop),
    ("skills", "List the skills this mind can run", cmd_skills),
    ("skill", "Run a skill: /skill <name>", cmd_skill),
)

# ---------------------------------------------------------------------------
# Held decisions
# ---------------------------------------------------------------------------
async def handle_hitl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Report an approve or deny tap to whoever is holding the decision.

    The buttons were posted by hive-tools against a token it minted, so this
    reports and relays rather than deciding: the message ends up saying what
    hive-tools said, including its status when the call failed, because the
    operator's next question is always whether the thing actually happened.
    """
    query = update.callback_query

    if not _is_allowed_user(query.from_user.id):
        await query.answer("Not authorized.")
        return

    parsed = hitl.parse_callback(query.data or "")
    if parsed is None:
        await query.answer("Unknown action.")
        return
    action, token = parsed

    await query.answer()

    # The question stays on screen under the outcome; only the buttons go, so
    # a second tap cannot report a decision twice.
    original = query.message.text or ""
    body = original.split("\n\n", 1)[-1] if "\n\n" in original else original
    with contextlib.suppress(Exception):
        await query.edit_message_text(f"Working\u2026\n\n{body}", reply_markup=None)

    url, payload, headers = hitl.respond_request(token, action)
    try:
        async with http.post(url, json=payload, headers=headers) as resp:
            label = hitl.outcome_label(action, resp.status, await resp.text())
    except Exception as exc:  # noqa: BLE001
        log_event(
            log, "surface.hitl.unreachable", level=logging.WARNING, surface="telegram",
            error=str(exc), error_type=type(exc).__name__,
        )
        label = f"Could not reach the approval service: {type(exc).__name__}"

    log_event(
        log, "surface.hitl.answered", surface="telegram",
        user_id=query.from_user.id, client_ref=query.message.chat_id,
    )
    with contextlib.suppress(Exception):
        await query.edit_message_text(f"{label}\n\n{body}", reply_markup=None)


# ---------------------------------------------------------------------------
# The extension seam
# ---------------------------------------------------------------------------
# A mind may carry a command no other mind has. It does not get its own bot to
# do that in: the core owns the process, the handler table and the slash menu,
# and a mind hands it additional commands before the surface starts. One bot
# per mind, one place where the table is assembled.
#
# Registering after the application is built raises rather than being ignored.
# A dropped registration is the worst available outcome — the command is in
# neither the menu nor the handler table, or in one and not the other, and the
# mind looks like it lost a feature for no reason anybody can see.
_EXTRA_COMMANDS: list[tuple[str, str, object]] = []
_TABLE_SEALED = False


class CommandsSealed(RuntimeError):
    """Raised when a command is registered after the surface has been built."""


def register_command(name: str, description: str, handler) -> None:
    """Add one of this mind's own commands to the table.

    Call before `run_telegram_bot`. A name already in the table — core or
    extension — raises: silently shadowing `/sessions` would take the picker
    away from the operator with nothing in the log about it.
    """
    if _TABLE_SEALED:
        raise CommandsSealed(
            f"/{name} registered after the Telegram surface was built; "
            "register this mind's commands before starting it"
        )
    taken = {existing for existing, _d, _h in all_commands()}
    if name in taken:
        raise ValueError(f"/{name} is already in the command table")
    _EXTRA_COMMANDS.append((name, description, handler))


def all_commands() -> tuple[tuple[str, str, object], ...]:
    """The core table plus whatever this mind registered."""
    return COMMANDS + tuple(_EXTRA_COMMANDS)


# Telegram's documented limits. A single malformed entry makes the whole
# `set_my_commands` call fail, which takes the menu down for every command at
# once — the bot runs on perfectly, offering nothing to tap, which is exactly
# the state this feature was written to get out of.
COMMAND_NAME_MAX = 32
COMMAND_DESCRIPTION_MAX = 256
_COMMAND_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


def valid_menu_entries() -> list[tuple[str, str]]:
    """The table entries Telegram will accept, with the rest named in the log.

    Enforced here rather than only in a test, because the cost of one bad
    entry is not that entry: `set_my_commands` rejects the whole batch, so a
    single typo silently removes the menu for all sixteen commands. Dropping
    the offender keeps the other fifteen tappable.
    """
    good: list[tuple[str, str]] = []
    for name, description, _handler in all_commands():
        text = (description or "").strip()
        if (
            1 <= len(name) <= COMMAND_NAME_MAX
            and set(name) <= _COMMAND_NAME_CHARS
            and 1 <= len(text) <= COMMAND_DESCRIPTION_MAX
        ):
            good.append((name, text))
        else:
            log_event(
                log, "surface.command.menu.entry.rejected", level=logging.WARNING,
                surface="telegram", command=name,
            )
    return good


def command_menu() -> list:
    """The table as Telegram wants it, for `set_my_commands`."""
    from telegram import BotCommand

    return [BotCommand(name, description) for name, description in valid_menu_entries()]


def _build_application(token: str):
    """Build the Telegram Application with all handlers wired up."""
    app = (
        ApplicationBuilder()
        .token(token)
        .concurrent_updates(True)
        .connect_timeout(10.0)
        .read_timeout(20.0)
        .write_timeout(20.0)
        .pool_timeout(5.0)
        .get_updates_read_timeout(30.0)
        .post_init(_on_startup)
        .post_shutdown(_on_shutdown)
        .build()
    )

    app.add_error_handler(_on_error)

    global _TABLE_SEALED
    _TABLE_SEALED = True
    for name, _description, handler in all_commands():
        app.add_handler(CommandHandler(name, with_typing(handler)))
    # Both patterned, and the held decision first. A handler registered with
    # no pattern takes every callback the chat produces — which is how the
    # picker ends up answering a button it knows nothing about, and how an
    # approval tap gets read as a request to switch conversations.
    app.add_handler(CallbackQueryHandler(
        with_typing(handle_hitl_callback), pattern=hitl.CALLBACK_PATTERN,
    ))
    app.add_handler(CallbackQueryHandler(
        with_typing(on_session_button), pattern=session_picker.CALLBACK_PATTERN,
    ))
    # The same handler, because that is where the single-use claim, the
    # keyboard removal and the acknowledgement live — a model picker left
    # tappable in scrollback would switch a conversation weeks later.
    app.add_handler(CallbackQueryHandler(
        with_typing(on_session_button), pattern=model_picker.CALLBACK_PATTERN,
    ))
    app.add_handler(MessageHandler(filters.PHOTO, with_typing(handle_photo)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, with_typing(handle_text)))
    app.add_handler(MessageHandler(filters.VOICE, with_typing(handle_voice)))
    # Catch-all: any /command not matched above is routed as a prompt
    app.add_handler(MessageHandler(filters.COMMAND, with_typing(handle_unknown_command)))

    return app


async def _proactive_poll_source() -> None:
    """Poll the mind backend for unsolicited turns and put them on the queue.

    For a surface running in its own container: it shares no memory with its
    mind, so the turns the harness produced with nobody listening sit behind
    the backend's own endpoint instead. This only moves them onto the queue —
    `_proactive_consumer` still does the delivering, so a polled turn gets the
    same chunking, the same backoff and the same journalling at shutdown as
    one handed over in process. Writing the sends here instead would be a
    second delivery path, and the one feature these answers need is that
    nothing loses them quietly.

    Nothing kills the loop. An unreachable backend, a non-200, a body that is
    not a list — each costs this cycle and nothing else, because the surface
    goes on carrying conversations whether or not the proactive channel is
    healthy.
    """
    url = f"{config.proactive_poll_url.rstrip('/')}/proactive"
    headers = {"Authorization": f"Bearer {COMMS_BEARER_TOKEN}"} if COMMS_BEARER_TOKEN else {}
    interval = config.proactive_poll_interval_s
    log_event(
        log, "surface.proactive.poll.started", surface="telegram",
        url=url, interval_s=interval,
    )

    from hive_surfaces import proactive

    while True:
        try:
            await asyncio.sleep(interval)
            async with http.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    log_event(
                        log, "surface.proactive.poll.rejected", level=logging.WARNING,
                        surface="telegram", url=url, status=resp.status,
                    )
                    continue
                items = await resp.json()
            for item in items or []:
                chat_id = item.get("chat_id")
                text = item.get("text")
                # A turn with no destination cannot be delivered and must not
                # be dropped silently — it is the only copy, the backend has
                # already let go of it.
                if not chat_id or not text:
                    log_event(
                        log, "surface.proactive.poll.unroutable", level=logging.WARNING,
                        surface="telegram", client_ref=chat_id or 0,
                        content_chars=len(text or ""),
                    )
                    continue
                proactive.enqueue(int(chat_id), text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log_event(
                log, "surface.proactive.poll.failed", level=logging.WARNING,
                surface="telegram", url=url, error=str(exc),
                error_type=type(exc).__name__,
            )


async def _proactive_consumer(app) -> None:
    """Drain the proactive queue and post unsolicited turns to Telegram.

    Runs for the lifetime of the bot. Each ``(chat_id, text)`` item is an
    assistant turn produced without an inbound user message (background agent
    completion, scheduled wakeup, rotation notice). Splits messages over
    Telegram's 4096-char limit; a failed send never kills the loop.
    """
    from hive_surfaces import proactive

    while True:
        chat_id, text, attempts = await proactive.get()
        chunks = _chunk_message(text)
        sent = 0
        try:
            while sent < len(chunks):
                await app.bot.send_message(chat_id=chat_id, text=chunks[sent])
                sent += 1
            continue
        except asyncio.CancelledError:
            # The item was taken off the queue and lives only in this frame, so
            # a cancellation mid-send drops it — and `_on_shutdown`'s drain
            # cannot see what is no longer in the queue. Put it back, then go.
            proactive.enqueue(chat_id, "".join(chunks[sent:]), attempts)
            raise
        except Exception as exc:  # noqa: BLE001
            attempts += 1
            # Whatever already landed is not sent again.
            remaining = "".join(chunks[sent:])
            if attempts >= _PROACTIVE_MAX_ATTEMPTS:
                log_event(
                    log, "surface.proactive.abandoned", level=logging.WARNING,
                    surface="telegram", client_ref=chat_id, error=str(exc),
                    content_chars=len(remaining), attempt=attempts,
                )
                # The text goes in the record. It is the last copy — the queue
                # is process memory and the operator never saw it — so a line
                # naming only the failure loses the answer for good.
                log.warning("undelivered text for chat %s: %s", chat_id, remaining)
                continue
            log_event(
                log, "surface.proactive.retry", level=logging.WARNING,
                surface="telegram", client_ref=chat_id, error=str(exc),
                attempt=attempts,
            )
            # Back to the tail, not retried in place: an item that cannot be
            # delivered must not hold up the ones behind it for three hours.
            # The wait rides on the item, not on the consumer. Sleeping here
            # made the retry interval global: one message with nowhere to go
            # delayed every other answer by three minutes, and a queue of N
            # items shed one attempt per interval rather than one per item.
            proactive.enqueue(chat_id, remaining, attempts, delay=_PROACTIVE_RETRY_S)


async def run_telegram_bot() -> None:
    """Async entry point — embeds the bot inside a larger asyncio program.

    Initialises the Application, starts long-polling, blocks until cancelled
    (e.g. on SIGTERM), then cleans up.
    """
    import asyncio as _asyncio

    token = _get_bot_token()
    app = _build_application(token)

    await app.initialize()
    # post_init/post_shutdown hooks only fire under run_polling/run_webhook —
    # invoke them explicitly when embedding the app in a custom event loop.
    await _on_startup(app)
    await app.start()
    await app.updater.start_polling()

    # One consumer, however the turns arrived. A surface sharing a process
    # with its mind is handed them in memory; one in its own container polls
    # the backend and puts them on the same queue.
    background = [_asyncio.ensure_future(_proactive_consumer(app))]
    if config.proactive_poll_url:
        background.append(_asyncio.ensure_future(_proactive_poll_source()))

    try:
        await _asyncio.Event().wait()
    except _asyncio.CancelledError:
        pass
    finally:
        for task in background:
            task.cancel()
        for task in background:
            try:
                await task
            except _asyncio.CancelledError:
                pass
        await app.updater.stop()
        await app.stop()
        await _on_shutdown(app)
        await app.shutdown()


if __name__ == "__main__":
    # The same entry point the service uses. `app.run_polling()` never starts
    # `_proactive_consumer`, so every answer `_deliver` could not send queued
    # into something nobody drained.
    asyncio.run(run_telegram_bot())
