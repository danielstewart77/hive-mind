"""
Hive Mind Discord Bot.

Thin HTTP client to the gateway server (server.py).
All Claude Code interaction flows through the gateway — no SDK dependency.
"""

import asyncio
import contextlib
import logging
import os
import re
import sys
import tempfile
import time

import aiohttp
import discord
from discord import app_commands

from config import config
from core.gateway_client import GatewayClient, get_lock, get_skills, time_ago

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("hive-mind-discord")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DISCORD_MSG_LIMIT = 2000
SERVER_URL = os.environ.get("HIVE_MIND_SERVER_URL", f"http://localhost:{config.server_port}")
VOICE_SERVER_URL = os.environ.get("VOICE_SERVER_URL", "http://localhost:8422")

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


# ---------------------------------------------------------------------------
# Voice / TTS helpers
# ---------------------------------------------------------------------------

async def _tts(text: str) -> bytes:
    """POST text to voice-server /tts, return OGG audio bytes."""
    async with http.post(f"{VOICE_SERVER_URL}/tts", json={"text": text}) as resp:
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
        ogg_bytes = await _tts(text)
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

    return chunks or ["(No response)"]


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

    async for text_chunk in gateway.query_stream(user_id, channel_id, prompt):
        accumulated += ("\n\n" if accumulated else "") + text_chunk
        now = time.monotonic()
        if now - last_edit >= edit_interval:
            preview = _chunk_message(accumulated)[0]
            with contextlib.suppress(discord.HTTPException):
                await sent.edit(content=preview)
            last_edit = now

    if not accumulated:
        accumulated = "(No response)"

    chunks = _chunk_message(accumulated)
    with contextlib.suppress(discord.HTTPException):
        await sent.edit(content=chunks[0])
    for extra in chunks[1:]:
        await sent.channel.send(extra)

    return accumulated


# ---------------------------------------------------------------------------
# Server commands
# ---------------------------------------------------------------------------
SERVER_COMMANDS = {"/clear", "/model", "/autopilot", "/kill", "/status", "/sessions", "/switch", "/new"}


async def _handle_server_command(content: str, user_id: int, channel_id: int) -> str:
    parts = content.split()
    cmd = parts[0]

    result = await gateway.server_command(user_id, channel_id, content)

    if "error" in result:
        return f"Error: {result['error']}"

    if cmd == "/sessions":
        return _format_sessions(result)
    if cmd == "/status":
        return _format_status(result)
    if cmd == "/new":
        return f"New session started: `{result.get('id', '?')[:8]}`"
    if cmd == "/clear":
        return f"Session cleared. New session: `{result.get('id', '?')[:8]}`"
    if cmd == "/model":
        if isinstance(result, list):
            lines = ["**Available models:**"]
            for m in result:
                lines.append(f"- `{m['name']}` ({m['provider']})")
            lines.append("\n`/model <name>` to switch")
            return "\n".join(lines)
        msg = f"Switched to **{result.get('model')}**"
        if result.get("warning"):
            msg += f"\n\u26a0\ufe0f {result['warning']}"
        return msg
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
        gateway = GatewayClient(http, SERVER_URL, "discord")
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
    log.info("Gateway: %s", SERVER_URL)
    log.info("Allowed users: %s", config.discord_allowed_users)


@bot.event
async def on_message(message: discord.Message):
    if message.author.id == bot.user.id:
        return
    if not _is_allowed_user(message.author.id):
        return

    is_dm = isinstance(message.channel, discord.DMChannel)
    if not is_dm and bot.user not in message.mentions:
        return
    if not is_dm and not _is_allowed_channel(message.channel.id):
        return

    content = message.content
    if bot.user:
        content = content.replace(f"<@{bot.user.id}>", "").strip()
        content = content.replace(f"<@!{bot.user.id}>", "").strip()

    if not content:
        return

    channel_id = message.channel.id
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
def _get_bot_token() -> str:
    """Load Discord bot token — keyring first, env fallback."""
    try:
        import keyring
        token = keyring.get_password("hive-mind", "DISCORD_BOT_TOKEN")
        if token:
            return token
    except Exception:
        pass
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        log.error("DISCORD_BOT_TOKEN not found in keyring or environment.")
        sys.exit(1)
    return token


if __name__ == "__main__":
    token = _get_bot_token()
    log.info("Starting Hive Mind Discord bot (gateway=%s)", SERVER_URL)
    bot.run(token, log_handler=None)
