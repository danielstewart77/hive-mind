"""
Hive Mind Discord Bot.

Thin HTTP client to the gateway server (server.py).
All Claude Code interaction flows through the gateway — no SDK dependency.
"""

import asyncio
import glob
import json
import logging
import os
import re
import sys
from datetime import datetime

import aiohttp
import discord
from discord import app_commands

from config import config

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

# Persistent HTTP session (created in setup_hook)
http: aiohttp.ClientSession | None = None

# Per-channel processing locks
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


def _get_lock(channel_id: int) -> asyncio.Lock:
    if channel_id not in _locks:
        _locks[channel_id] = asyncio.Lock()
    return _locks[channel_id]


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
# Gateway client helpers
# ---------------------------------------------------------------------------
async def _ensure_session(user_id: int, channel_id: int) -> str:
    """Get active session for this channel, or create one."""
    # Check for existing active session
    async with http.get(
        f"{SERVER_URL}/sessions",
        params={"client_type": "discord", "client_ref": str(channel_id)},
    ) as resp:
        data = await resp.json()
        # Find one that's active for this channel
        for s in data:
            if s.get("is_active"):
                return s["id"]

    # Create new session
    async with http.post(
        f"{SERVER_URL}/sessions",
        json={
            "owner_type": "discord",
            "owner_ref": str(user_id),
            "client_ref": str(channel_id),
        },
    ) as resp:
        return (await resp.json())["id"]


async def _query(prompt: str, user_id: int, channel_id: int) -> str:
    """Send message to gateway, stream SSE response, return text."""
    session_id = await _ensure_session(user_id, channel_id)
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

    # Prefer collected assistant blocks (all turns); fall back to result event text
    return "\n\n".join(assistant_texts) or result_text or "(No response)"


async def _server_command(
    cmd: str, user_id: int, channel_id: int
) -> dict:
    """Send a server command and return the JSON response."""
    async with http.post(
        f"{SERVER_URL}/command",
        json={
            "content": cmd,
            "owner_type": "discord",
            "owner_ref": str(user_id),
            "client_ref": str(channel_id),
        },
    ) as resp:
        return await resp.json()


# ---------------------------------------------------------------------------
# Server command formatters
# ---------------------------------------------------------------------------
def _format_sessions(sessions: list[dict]) -> str:
    """Format session list for Discord display."""
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

        # Time ago
        last = s.get("last_active", 0)
        ago = _time_ago(last) if last else "?"

        lines.append(
            f"{i}. {status_icon}{autopilot} `{short_id}` — \"{summary}\" [{s.get('model', '?')}] ({ago})"
        )

    lines.append("")
    lines.append("`/switch <number>` to resume \u00b7 `/new` to start \u00b7 `/kill <number>` to kill")
    return "\n".join(lines)


def _time_ago(ts: float) -> str:
    delta = datetime.now().timestamp() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta / 60)} min ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


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
# Server commands handled by the bot
# ---------------------------------------------------------------------------
SERVER_COMMANDS = {"/clear", "/model", "/autopilot", "/kill", "/status", "/sessions", "/switch", "/new"}


async def _handle_server_command(
    content: str, user_id: int, channel_id: int
) -> str:
    """Handle a server command, return formatted Discord message."""
    parts = content.split()
    cmd = parts[0]

    result = await _server_command(content, user_id, channel_id)

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
            # Model listing
            lines = ["**Available models:**"]
            for m in result:
                lines.append(f"- `{m['name']}` ({m['provider']})")
            lines.append("\n`/model <name>` to switch")
            return "\n".join(lines)
        # Model switch result
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

    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Discord Bot
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True


class HiveMindBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        global http
        http = aiohttp.ClientSession()
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
    msg = await _handle_server_command(
        "/sessions", interaction.user.id, interaction.channel_id
    )
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="new", description="Start a new Hive Mind session")
async def cmd_new(interaction: discord.Interaction):
    if not _is_allowed_user(interaction.user.id):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    msg = await _handle_server_command(
        "/new", interaction.user.id, interaction.channel_id
    )
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="clear", description="Clear session and start fresh")
async def cmd_clear(interaction: discord.Interaction):
    if not _is_allowed_user(interaction.user.id):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    msg = await _handle_server_command(
        "/clear", interaction.user.id, interaction.channel_id
    )
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="status", description="Show Hive Mind status")
async def cmd_status(interaction: discord.Interaction):
    if not _is_allowed_user(interaction.user.id):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    msg = await _handle_server_command(
        "/status", interaction.user.id, interaction.channel_id
    )
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="model", description="List or switch model")
@app_commands.describe(name="Model name to switch to (omit to list)")
async def cmd_model(interaction: discord.Interaction, name: str = None):
    if not _is_allowed_user(interaction.user.id):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    cmd = f"/model {name}" if name else "/model"
    msg = await _handle_server_command(
        cmd, interaction.user.id, interaction.channel_id
    )
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="autopilot", description="Toggle autopilot mode")
async def cmd_autopilot(interaction: discord.Interaction):
    if not _is_allowed_user(interaction.user.id):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    msg = await _handle_server_command(
        "/autopilot", interaction.user.id, interaction.channel_id
    )
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="switch", description="Switch to a different session")
@app_commands.describe(target="Session number or ID")
async def cmd_switch(interaction: discord.Interaction, target: str):
    if not _is_allowed_user(interaction.user.id):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    msg = await _handle_server_command(
        f"/switch {target}", interaction.user.id, interaction.channel_id
    )
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="kill", description="Kill a session")
@app_commands.describe(target="Session number or ID")
async def cmd_kill(interaction: discord.Interaction, target: str):
    if not _is_allowed_user(interaction.user.id):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    msg = await _handle_server_command(
        f"/kill {target}", interaction.user.id, interaction.channel_id
    )
    await interaction.followup.send(msg, ephemeral=True)


# ---------------------------------------------------------------------------
# /skills — list all available skills
# ---------------------------------------------------------------------------
@bot.tree.command(name="skills", description="List all available Claude skills")
async def cmd_skills(interaction: discord.Interaction):
    if not _is_allowed_user(interaction.user.id):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    skills = _get_skills()
    if not skills:
        await interaction.response.send_message("No skills found.", ephemeral=True)
        return
    lines = ["**Available Skills**\n"]
    for s in skills:
        hint = f" `{s['argument_hint']}`" if s["argument_hint"] else ""
        lines.append(f"• **{s['name']}**{hint} — {s['description']}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ---------------------------------------------------------------------------
# /skill — dynamic skill invocation with autocomplete
# ---------------------------------------------------------------------------
async def _autocomplete_skill_name(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=s["name"], value=s["name"])
        for s in _get_skills()
        if current.lower() in s["name"].lower()
    ][:25]


async def _autocomplete_skill_args(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    skill_name = interaction.namespace.name  # type: ignore[attr-defined]
    if not skill_name:
        return []
    hint = next(
        (s["argument_hint"] for s in _get_skills() if s["name"] == skill_name), ""
    )
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
    response = await _query(prompt, interaction.user.id, interaction.channel_id)
    chunks = _chunk_message(response)
    await interaction.followup.send(chunks[0])
    for chunk in chunks[1:]:
        await interaction.channel.send(chunk)


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
    lock = _get_lock(channel_id)

    if lock.locked():
        await message.reply("Still processing your previous message, please wait.")
        return

    async with lock:
        try:
            # Check if it's a server command sent as a regular message
            parts = content.split()
            if parts and parts[0] in SERVER_COMMANDS:
                async with message.channel.typing():
                    response = await _handle_server_command(
                        content, message.author.id, channel_id
                    )
            else:
                async with message.channel.typing():
                    response = await _query(content, message.author.id, channel_id)

            chunks = _chunk_message(response)
            await message.reply(chunks[0])
            for chunk in chunks[1:]:
                await message.channel.send(chunk)

        except Exception:
            log.exception("Error processing message in channel %s", channel_id)
            await message.reply("Something went wrong. Try again or use /clear.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _get_bot_token() -> str:
    """Load Discord bot token from keyring with .env fallback."""
    token = os.getenv("DISCORD_BOT_TOKEN")
    if token:
        return token
    try:
        import keyring

        token = keyring.get_password("hive-mind", "DISCORD_BOT_TOKEN")
    except Exception:
        pass
    if not token:
        log.error(
            "DISCORD_BOT_TOKEN not found. Set it via:\n"
            "  - .env file: DISCORD_BOT_TOKEN=your-token\n"
            '  - keyring: python -c "import keyring; keyring.set_password(\'hive-mind\', \'DISCORD_BOT_TOKEN\', \'your-token\')"'
        )
        sys.exit(1)
    return token


if __name__ == "__main__":
    token = _get_bot_token()
    log.info("Starting Hive Mind Discord bot (gateway=%s)", SERVER_URL)
    bot.run(token, log_handler=None)
