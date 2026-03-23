"""
Hive Mind Group Chat Discord Bot (skeleton).

Routes messages through group sessions for multi-mind conversations.
Token not yet provisioned -- this is a structural skeleton only.
"""

import json
import logging
import os
from typing import Optional

import aiohttp
import discord

from config import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("hive-mind-group-bot")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DISCORD_MSG_LIMIT = 2000
SERVER_URL = os.environ.get(
    "HIVE_MIND_SERVER_URL", f"http://localhost:{config.server_port}"
)

# Group session state: channel_id -> group_session_id
_active_group_sessions: dict[int, str] = {}


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _get_bot_token() -> Optional[str]:
    """Get bot token -- keyring first, env fallback."""
    try:
        import keyring
        token = keyring.get_password("hive-mind", "HIVEMIND_DISCORD_TOKEN")
        if token:
            return token
    except Exception:
        pass
    return os.getenv("HIVEMIND_DISCORD_TOKEN")


def _is_allowed_user(user_id: int) -> bool:
    """Fail-closed: empty allowlist = no access."""
    return user_id in config.discord_allowed_users


# ---------------------------------------------------------------------------
# Group session management
# ---------------------------------------------------------------------------
class HiveMindGroupBot:
    """Bot skeleton for multi-mind group conversations."""

    def __init__(self) -> None:
        self.http: Optional[aiohttp.ClientSession] = None

    async def _ensure_http(self) -> aiohttp.ClientSession:
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession()
        return self.http

    async def create_group_session(
        self, channel_id: int, moderator: str = "ada"
    ) -> str:
        """Create a new group session via the gateway."""
        http = await self._ensure_http()
        async with http.post(
            f"{SERVER_URL}/group-sessions",
            json={"moderator_mind_id": moderator},
        ) as resp:
            data = await resp.json()
            group_session_id = data["id"]
            _active_group_sessions[channel_id] = group_session_id
            log.info(
                "Created group session %s for channel %d",
                group_session_id, channel_id,
            )
            return group_session_id

    async def send_group_message(
        self, channel_id: int, content: str
    ) -> list[str]:
        """Send a message to the group session for this channel.

        Returns a list of response text chunks with mind attribution.
        """
        group_session_id = _active_group_sessions.get(channel_id)
        if not group_session_id:
            group_session_id = await self.create_group_session(channel_id)

        http = await self._ensure_http()
        chunks: list[str] = []

        sse_timeout = aiohttp.ClientTimeout(total=0, sock_read=0)
        async with http.post(
            f"{SERVER_URL}/group-sessions/{group_session_id}/message",
            json={"content": content},
            timeout=sse_timeout,
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
                        for block in event.get("message", {}).get("content", []):
                            if block.get("type") == "text" and block.get("text"):
                                chunks.append(block["text"])

        return chunks

    async def handle_new_command(self, channel_id: int) -> str:
        """Handle /new -- create fresh group session."""
        group_session_id = await self.create_group_session(channel_id)
        return f"New group session created: {group_session_id}"

    async def close(self) -> None:
        """Clean up HTTP session."""
        if self.http and not self.http.closed:
            await self.http.close()


# ---------------------------------------------------------------------------
# Discord bot setup (skeleton -- token not yet provisioned)
# ---------------------------------------------------------------------------
def _build_bot() -> discord.Client:
    """Build the Discord client. Token binding deferred."""
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    return client


# Entry point (will be wired when token is provisioned)
if __name__ == "__main__":
    token = _get_bot_token()
    if not token:
        log.error("HIVEMIND_DISCORD_TOKEN not configured -- cannot start bot")
    else:
        bot = _build_bot()
        bot.run(token)
