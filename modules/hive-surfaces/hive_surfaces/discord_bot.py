"""
Hive Mind Discord Bot.

Thin HTTP client to the gateway server (server.py).
All Claude Code interaction flows through the gateway — no SDK dependency.
"""

import asyncio
import contextlib
import logging
import os
import tempfile
import time

import aiohttp
import discord
from discord import app_commands

from hive_surfaces import effort_picker, model_picker, token_store, voice_routing
from hive_surfaces.config import config
from hive_surfaces.gateway_client import GatewayClient
from hive_surfaces.bot_utils import get_lock, time_ago
from hive_surfaces.skills import get_skills
from hive_surfaces.hive_logging import configure_logging, log_event

log = configure_logging("hive-mind-discord")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DISCORD_MSG_LIMIT = 2000
COMMS_URL = os.environ.get("COMMS_URL", "http://127.0.0.1:8426")
COMMS_BEARER_TOKEN = os.environ.get("COMMS_BEARER_TOKEN")
VOICE_COMMS_URL = os.environ.get("VOICE_COMMS_URL", "http://localhost:8422")
# A host running both engines names each one's server; a host running one
# names neither and `VOICE_COMMS_URL` answers for whichever engine its minds
# picked. Which of the two a given mind is spoken by comes off that mind's own
# record, never off this environment.
VOICE_SERVER_URL_CHATTERBOX = os.environ.get("VOICE_SERVER_URL_CHATTERBOX", "")
VOICE_SERVER_URL_KOKORO = os.environ.get("VOICE_SERVER_URL_KOKORO", "")

# Persistent HTTP session and gateway client (created in setup_hook)
http: aiohttp.ClientSession | None = None
gateway: GatewayClient | None = None

# Active voice clients keyed by guild ID
_voice_clients: dict[int, discord.VoiceClient] = {}


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _is_allowed_user(user_id: int) -> bool:
    """Fail-closed: empty allowlist = no access."""
    return user_id in config.discord_allowed_users


def _is_allowed_channel(channel_id: int) -> bool:
    """Empty list = all channels allowed."""
    if not config.discord_allowed_channels:
        return True
    return channel_id in config.discord_allowed_channels


def task_channels() -> set[int]:
    """Channel ids this mind answers in without an at-mention.

    The host supplies either the ids or a callable returning them, and the
    callable is called per message rather than once at configure time: a
    host deriving these from skills on disk wants a channel added to a skill
    to start working inside its own cache window rather than at the next
    restart.

    A resolver that fails means this process cannot tell which channels are
    resident, and the safe reading of that is none of them — the surface
    falls back to requiring a mention, which is quiet, rather than treating
    every channel as resident, which would have the mind answering strangers
    in rooms it was never addressed in. Logged, never raised: this runs on
    every inbound message, and a mind that stops answering because a listing
    failed is a worse outcome than one that asks to be named.
    """
    declared = config.discord_task_channels
    if callable(declared):
        try:
            declared = declared()
        except Exception as exc:  # noqa: BLE001
            log_event(
                log, "surface.resident_channels.unresolved", level=logging.WARNING,
                surface="discord", error=str(exc), error_type=type(exc).__name__,
            )
            return set()
    return {int(c) for c in declared}


def conversation_channel_id(channel, task_channel_ids: set[int]) -> int:
    """The channel whose conversation a message belongs to.

    A thread has its own id, so a reply threaded under a resident channel
    would otherwise start a second conversation holding none of the history
    the reply is about — and the mind would answer it coherently while
    having no idea what it was about. A thread hanging off a resident
    channel folds into that channel; every other thread keeps its own id,
    since a thread in an ordinary channel is genuinely its own topic.
    """
    parent_id = getattr(channel, "parent_id", None)
    if parent_id in task_channel_ids:
        return parent_id
    return channel.id


def should_handle_message(
    *, is_dm: bool, mentioned: bool, channel_id: int, task_channel_ids: set[int]
) -> bool:
    """Whether an inbound message is addressed to this mind.

    A DM always is. An ordinary guild channel needs an explicit mention,
    because the bot is one member of a room full of people talking to each
    other. A resident channel is the opposite case: it exists for one
    conversation with one mind.

    A resident channel also stands outside ``discord_allowed_channels``. A
    channel named as resident is a stronger statement of intent than a list
    somebody has to remember to update.
    """
    if is_dm:
        return True
    if channel_id in task_channel_ids:
        return True
    return mentioned


# ---------------------------------------------------------------------------
# Voice / TTS helpers
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
    fallback_url=VOICE_COMMS_URL,
)


async def _tts(text: str, voice_id: str) -> bytes:
    """POST text to voice-server /tts, return OGG audio bytes.

    `voice_id` names the mind, which decides both the server called and the
    voice spoken: under chatterbox it selects that mind's reference clip,
    under kokoro the catalogued voice its record names.
    """
    async with http.post(
        f"{_voice_servers.resolve(voice_id)}/tts",
        json={"text": text, "voice_id": voice_id},
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"TTS error {resp.status}: {await resp.text()}")
        return await resp.read()


async def _play_tts_for_member(member: discord.Member | discord.User, text: str) -> None:
    """Synthesise text and play it in the member's current voice channel (if any)."""
    if not isinstance(member, discord.Member):
        return  # DMs have no voice channel
    if not member.voice or not member.voice.channel:
        return  # User not in a voice channel

    voice_channel = member.voice.channel
    guild_id = member.guild.id

    vc = _voice_clients.get(guild_id)
    try:
        if vc is None or not vc.is_connected():
            vc = await voice_channel.connect()
            _voice_clients[guild_id] = vc
        elif vc.channel != voice_channel:
            await vc.move_to(voice_channel)
    except Exception:
        log.exception("Failed to connect to voice channel in guild %s", guild_id)
        return

    try:
        ogg_bytes = await _tts(text, voice_id=gateway.mind_id)
    except Exception:
        log.exception("TTS synthesis failed")
        return

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        f.write(ogg_bytes)
        tmp_path = f.name

    try:
        if vc.is_playing():
            vc.stop()

        loop = asyncio.get_event_loop()
        done = asyncio.Event()

        def _after(error):
            if error:
                log.warning("Voice playback error: %s", error)
            loop.call_soon_threadsafe(done.set)

        vc.play(discord.FFmpegPCMAudio(tmp_path), after=_after)
        await asyncio.wait_for(done.wait(), timeout=120.0)
    except asyncio.TimeoutError:
        log.warning("Voice playback timed out in guild %s", guild_id)
        vc.stop()
    except Exception:
        log.exception("Voice playback failed in guild %s", guild_id)
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Server command formatters
# ---------------------------------------------------------------------------
def _format_sessions(sessions: list[dict]) -> str:
    if not sessions:
        return "No sessions found."

    lines = ["**Your Sessions:**"]
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
            f"{i}. {status_icon}{autopilot} `{short_id}` — \"{summary}\" [{s.get('model', '?')}] ({ago})"
        )

    lines.append("")
    lines.append("`/switch <number>` to resume \u00b7 `/new` to start \u00b7 `/kill <number>` to kill")
    return "\n".join(lines)


def _format_status(data: dict) -> str:
    return (
        f"**Server port:** {data.get('server_port')}\n"
        f"**Default model:** {data.get('default_model')}\n"
        f"**Sessions:** {data.get('running_sessions')}/{data.get('total_sessions')} running"
    )


# ---------------------------------------------------------------------------
# Message chunking for Discord's 2000-char limit
# ---------------------------------------------------------------------------
def _chunk_message(text: str) -> list[str]:
    """Split text into <=2000 char chunks, preserving code fences."""
    if len(text) <= DISCORD_MSG_LIMIT:
        return [text]

    chunks: list[str] = []
    current = ""
    in_code_block = False
    fence_lang = ""

    for line in text.split("\n"):
        stripped = line.strip()

        if stripped.startswith("```"):
            if not in_code_block:
                in_code_block = True
                fence_lang = stripped
            else:
                in_code_block = False

        candidate = current + line + "\n" if current else line + "\n"

        if len(candidate) > DISCORD_MSG_LIMIT:
            if current:
                if in_code_block:
                    current += "```\n"
                chunks.append(current.rstrip("\n"))
                current = fence_lang + "\n" if in_code_block else ""

            pending = line + "\n"
            prefix = (
                fence_lang + "\n"
                if in_code_block and not current.startswith("```")
                else current
            )
            pending = prefix + pending if prefix else pending
            current = ""

            while len(pending) > DISCORD_MSG_LIMIT:
                chunks.append(pending[:DISCORD_MSG_LIMIT])
                pending = pending[DISCORD_MSG_LIMIT:]
            current = pending
        else:
            current = candidate

    if current.strip():
        chunks.append(current.rstrip("\n"))

    return chunks or [
        "ERROR: empty response from gateway. "
        "Check the mind container logs for the real failure."
    ]


# ---------------------------------------------------------------------------
# Streaming helper
# ---------------------------------------------------------------------------
async def _stream_to_message(
    sent: discord.Message,
    user_id: int,
    channel_id: int,
    prompt: str,
    edit_interval: float = 1.0,
) -> str:
    """Stream a gateway response, progressively editing sent as chunks arrive.

    Returns the full accumulated text.
    """
    accumulated = ""
    last_edit = 0.0

    # Held for the whole stream rather than just the first reply: discord.py
    # refreshes the indicator while the block runs, so it keeps showing for as
    # long as the mind is working instead of lapsing after five seconds.
    async with sent.channel.typing():
        async for text_chunk in gateway.query_stream(user_id, channel_id, prompt):
            # Plain concatenation — the stream carries mid-word token deltas and
            # emits its own block separators.
            accumulated += text_chunk
            now = time.monotonic()
            if now - last_edit >= edit_interval:
                preview = _chunk_message(accumulated)[0]
                with contextlib.suppress(discord.HTTPException):
                    await sent.edit(content=preview)
                last_edit = now

    if not accumulated:
        accumulated = (
            "ERROR: mind stream closed with no text output. "
            "Check the mind container logs for the real failure."
        )

    chunks = _chunk_message(accumulated)
    with contextlib.suppress(discord.HTTPException):
        await sent.edit(content=chunks[0])
    for extra in chunks[1:]:
        await sent.channel.send(extra)

    return accumulated


# ---------------------------------------------------------------------------
# Server commands
# ---------------------------------------------------------------------------
SERVER_COMMANDS = {
    "/clear", "/model", "/effort", "/autopilot", "/kill", "/status", "/sessions", "/switch", "/new",
}


async def _handle_server_command(content: str, user_id: int, channel_id: int) -> str:
    parts = content.split()
    cmd = parts[0]

    log_event(
        log, "surface.command.received", surface="discord", command=cmd,
        user_id=user_id, client_ref=channel_id,
    )

    result = await gateway.server_command(user_id, channel_id, content)

    if "error" in result:
        log_event(
            log, "surface.command.failed", level=logging.WARNING, surface="discord",
            command=cmd, user_id=user_id, client_ref=channel_id,
        )
        return f"Error: {result['error']}"
    log_event(
        log, "surface.command.completed", surface="discord", command=cmd,
        user_id=user_id, client_ref=channel_id,
    )

    if cmd == "/sessions":
        return _format_sessions(result)
    if cmd == "/status":
        return _format_status(result)
    if cmd == "/new":
        return f"New session started: `{result.get('id', '?')[:8]}`"
    if cmd == "/clear":
        return f"Session cleared. New session: `{result.get('id', '?')[:8]}`"
    if cmd == "/model":
        # A bare `/model` answers `{"models": [...], "current": ...}`. Testing
        # for a list read that dict as a switch and printed "Switched to None".
        rows = model_picker.models_from(result)
        if rows:
            current = result.get("current") if isinstance(result, dict) else None
            lines = ["**Available models:**"]
            for m in rows:
                provider = m.get("provider_label") or m.get("provider")
                mark = " \u2190 current" if m["name"] == current else ""
                lines.append(f"- `{m['name']}`" + (f" ({provider})" if provider else "") + mark)
            lines.append("\n`/model <name>` to switch")
            return "\n".join(lines)
        if not result.get("model"):
            return "No model change was made \u2014 `/model` lists them."
        msg = f"Switched to **{result.get('model')}**"
        if result.get("warning"):
            msg += f"\n\u26a0\ufe0f {result['warning']}"
        return msg
    if cmd == "/effort":
        if "levels" in result:
            levels = effort_picker.levels_from(result)
            if not levels:
                return f"{result.get('model') or 'This model'} takes no effort setting."
            current = result.get("current")
            marked = [f"**{lv}** \u2190 current" if lv == current else lv for lv in levels]
            return (f"Effort for `{result.get('model')}`: " + ", ".join(marked)
                    + "\n`/effort <level>` to set")
        return effort_picker.format_effort_result(result)
    if cmd == "/autopilot":
        on = result.get("autopilot", False)
        summary = result.get("summary", "this session")
        if on:
            return f"\U0001f916 **Autopilot ON** for \"{summary}\"\n(Claude will execute all actions without asking)"
        return f"\U0001f512 **Autopilot OFF** for \"{summary}\"\n(Claude will ask for permission before risky actions)"
    if cmd == "/switch":
        return f"Resumed session \"{result.get('summary', '?')}\""
    if cmd == "/kill":
        return f"Killed session \"{result.get('summary', '?')}\" (status: {result.get('status')})"

    return "Done."


# ---------------------------------------------------------------------------
# Discord Bot
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True


class HiveMindBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        global http, gateway
        http = aiohttp.ClientSession()
        mind_id = os.environ["MIND_ID"]
        gateway = GatewayClient(
            http, COMMS_URL, f"discord:{mind_id}",
            surface_prompt=None,
            mind_id=mind_id,
            bearer_token=COMMS_BEARER_TOKEN or None,  # secret-guard: allow
        )
        await self.tree.sync()
        log.info("Slash commands synced")

    async def close(self):
        if http:
            await http.close()
        await super().close()


bot = HiveMindBot()


# ---------------------------------------------------------------------------
# Slash commands (Discord-native, route to gateway)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# The extension seam
# ---------------------------------------------------------------------------
# The Telegram counterpart assembles a table; Discord's command tree is built
# by decorator at import time, so a mind's own command is added to that same
# tree rather than to a list the core walks. One bot, one tree, same rule:
# registering after the client has started raises instead of being dropped,
# because discord.py only publishes the tree on sync and a command added
# afterwards is in the process but not in anyone's slash menu.
_STARTED = False


class CommandsSealed(RuntimeError):
    """Raised when a command is registered after the client has started."""


def register_discord_command(name: str, description: str, handler) -> None:
    """Add one of this mind's own slash commands. Call before `run_discord_bot`."""
    if _STARTED:
        raise CommandsSealed(
            f"/{name} registered after the Discord surface started; "
            "register this mind's commands before starting it"
        )
    if bot.tree.get_command(name) is not None:
        raise ValueError(f"/{name} is already in the command tree")
    bot.tree.command(name=name, description=description)(handler)


@bot.tree.command(name="sessions", description="List your Hive Mind sessions")
async def cmd_sessions(interaction: discord.Interaction):
    if not _is_allowed_user(interaction.user.id):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    msg = await _handle_server_command("/sessions", interaction.user.id, interaction.channel_id)
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="new", description="Start a new Hive Mind session")
async def cmd_new(interaction: discord.Interaction):
    if not _is_allowed_user(interaction.user.id):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    msg = await _handle_server_command("/new", interaction.user.id, interaction.channel_id)
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="clear", description="Clear session and start fresh")
async def cmd_clear(interaction: discord.Interaction):
    if not _is_allowed_user(interaction.user.id):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    msg = await _handle_server_command("/clear", interaction.user.id, interaction.channel_id)
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="status", description="Show Hive Mind status")
async def cmd_status(interaction: discord.Interaction):
    if not _is_allowed_user(interaction.user.id):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    msg = await _handle_server_command("/status", interaction.user.id, interaction.channel_id)
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="model", description="List or switch model")
@app_commands.describe(name="Model name to switch to (omit to list)")
async def cmd_model(interaction: discord.Interaction, name: str = None):
    if not _is_allowed_user(interaction.user.id):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    cmd = f"/model {name}" if name else "/model"
    msg = await _handle_server_command(cmd, interaction.user.id, interaction.channel_id)
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="autopilot", description="Toggle autopilot mode")
async def cmd_autopilot(interaction: discord.Interaction):
    if not _is_allowed_user(interaction.user.id):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    msg = await _handle_server_command("/autopilot", interaction.user.id, interaction.channel_id)
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="switch", description="Switch to a different session")
@app_commands.describe(target="Session number or ID")
async def cmd_switch(interaction: discord.Interaction, target: str):
    if not _is_allowed_user(interaction.user.id):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    msg = await _handle_server_command(f"/switch {target}", interaction.user.id, interaction.channel_id)
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="kill", description="Kill a session")
@app_commands.describe(target="Session number or ID")
async def cmd_kill(interaction: discord.Interaction, target: str):
    if not _is_allowed_user(interaction.user.id):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    msg = await _handle_server_command(f"/kill {target}", interaction.user.id, interaction.channel_id)
    await interaction.followup.send(msg, ephemeral=True)


# ---------------------------------------------------------------------------
# /skills — list all available skills
# ---------------------------------------------------------------------------
@bot.tree.command(name="skills", description="List all available Claude skills")
async def cmd_skills(interaction: discord.Interaction):
    if not _is_allowed_user(interaction.user.id):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    skills = get_skills()
    if not skills:
        await interaction.response.send_message("No skills found.", ephemeral=True)
        return
    lines = ["**Available Skills**\n"]
    for s in skills:
        hint = f" `{s['argument_hint']}`" if s["argument_hint"] else ""
        lines.append(f"• **{s['name']}**{hint} — {s['description']}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ---------------------------------------------------------------------------
# /skill — dynamic skill invocation with autocomplete + streaming
# ---------------------------------------------------------------------------
async def _autocomplete_skill_name(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=s["name"], value=s["name"])
        for s in get_skills()
        if current.lower() in s["name"].lower()
    ][:25]


async def _autocomplete_skill_args(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    skill_name = interaction.namespace.name  # type: ignore[attr-defined]
    if not skill_name:
        return []
    hint = next((s["argument_hint"] for s in get_skills() if s["name"] == skill_name), "")
    if not hint:
        return []
    return [app_commands.Choice(name=hint, value=current)]


@bot.tree.command(name="skill", description="Invoke a Claude skill")
@app_commands.describe(name="Skill to invoke", args="Arguments for the skill")
@app_commands.autocomplete(name=_autocomplete_skill_name, args=_autocomplete_skill_args)
async def cmd_skill(interaction: discord.Interaction, name: str, args: str = None):
    if not _is_allowed_user(interaction.user.id):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    await interaction.response.defer()
    prompt = f"/{name} {args}" if args else f"/{name}"
    sent = await interaction.followup.send("\u2026", wait=True)
    response = await _stream_to_message(sent, interaction.user.id, interaction.channel_id, prompt)
    await _play_tts_for_member(interaction.user, response)


# ---------------------------------------------------------------------------
# /join — join caller's voice channel
# ---------------------------------------------------------------------------
@bot.tree.command(name="join", description="Join your current voice channel for TTS")
async def cmd_join(interaction: discord.Interaction):
    if not _is_allowed_user(interaction.user.id):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    member = interaction.user
    if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
        await interaction.response.send_message("You're not in a voice channel.", ephemeral=True)
        return
    voice_channel = member.voice.channel
    guild_id = member.guild.id
    vc = _voice_clients.get(guild_id)
    try:
        if vc is None or not vc.is_connected():
            vc = await voice_channel.connect()
            _voice_clients[guild_id] = vc
        else:
            await vc.move_to(voice_channel)
        await interaction.response.send_message(f"Joined **{voice_channel.name}**.", ephemeral=True)
    except Exception:
        log.exception("Failed to join voice channel")
        await interaction.response.send_message("Failed to join voice channel.", ephemeral=True)


# ---------------------------------------------------------------------------
# /leave — disconnect from voice
# ---------------------------------------------------------------------------
@bot.tree.command(name="leave", description="Leave the current voice channel")
async def cmd_leave(interaction: discord.Interaction):
    if not _is_allowed_user(interaction.user.id):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    if not interaction.guild:
        await interaction.response.send_message("Not in a server.", ephemeral=True)
        return
    guild_id = interaction.guild.id
    vc = _voice_clients.pop(guild_id, None)
    if vc and vc.is_connected():
        await vc.disconnect()
        await interaction.response.send_message("Left voice channel.", ephemeral=True)
    else:
        await interaction.response.send_message("Not currently in a voice channel.", ephemeral=True)


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %s)", bot.user.name, bot.user.id)
    log.info("Gateway: %s", COMMS_URL)
    log.info("Allowed users: %s", config.discord_allowed_users)


@bot.event
async def on_message(message: discord.Message):
    if message.author.id == bot.user.id:
        return
    if not _is_allowed_user(message.author.id):
        log_event(
            log, "surface.auth.rejected", level=logging.WARNING, surface="discord",
            user_id=message.author.id, client_ref=message.channel.id,
        )
        return

    is_dm = isinstance(message.channel, discord.DMChannel)
    resident = task_channels()
    # The conversation id, not the raw channel id: a thread under a resident
    # channel is that channel's conversation, and the allow check, the lock
    # and the gateway all have to agree on which one that is.
    channel_id = conversation_channel_id(message.channel, resident)
    mentioned = bot.user in message.mentions
    if not should_handle_message(
        is_dm=is_dm, mentioned=mentioned,
        channel_id=channel_id, task_channel_ids=resident,
    ):
        return
    if not is_dm and channel_id not in resident and not _is_allowed_channel(channel_id):
        return

    content = message.content
    if bot.user:
        content = content.replace(f"<@{bot.user.id}>", "").strip()
        content = content.replace(f"<@!{bot.user.id}>", "").strip()

    if not content:
        return

    log_event(
        log, "surface.message.received", surface="discord", message_type="text",
        user_id=message.author.id, client_ref=channel_id, content_chars=len(content),
    )
    lock = get_lock(channel_id)

    if lock.locked():
        await message.reply("Still processing your previous message, please wait.")
        return

    async with lock:
        try:
            parts = content.split()
            if parts and parts[0] in SERVER_COMMANDS:
                async with message.channel.typing():
                    response = await _handle_server_command(content, message.author.id, channel_id)
                chunks = _chunk_message(response)
                await message.reply(chunks[0])
                for chunk in chunks[1:]:
                    await message.channel.send(chunk)
            else:
                async with message.channel.typing():
                    sent = await message.reply("\u2026")
                response = await _stream_to_message(sent, message.author.id, channel_id, content)
                await _play_tts_for_member(message.author, response)

        except Exception:
            log.exception("Error processing message in channel %s", channel_id)
            await message.reply("Something went wrong. Try again or use /clear.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _get_bot_token() -> str | None:
    """Load this surface's Discord token. None when it has none.

    By the same rule Telegram's reads by, out of the same table: the
    environment unless `DISCORD_BOT_TOKEN_KEYRING_KEY` names a keyring key,
    in which case that key wins, because the only reason to name one is that
    this surface's token is not the ambient one. Reading only the environment
    is how a mind whose token was in the keyring all along came up
    crashlooping on a surface it was told it had no token for.

    None rather than an exit, because a mind may legitimately run
    Telegram-only.
    """
    return token_store.resolve_token("discord") or None


async def run_discord_bot() -> None:
    """Async entry point — started as a coroutine alongside the mind server
    and Telegram surface in launch_mind_server_and_bots.py. No-ops when no
    DISCORD_BOT_TOKEN is configured."""
    token = _get_bot_token()
    if not token:
        log.info("DISCORD_BOT_TOKEN not set — Discord surface disabled.")
        return
    log.info("Starting Discord bot (gateway=%s)", COMMS_URL)
    global _STARTED
    _STARTED = True
    await bot.start(token)


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_discord_bot())
