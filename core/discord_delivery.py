"""Posting into a Discord channel without an inbound message to reply to.

`bots/discord_bot.py` only ever sends in reply to something it received —
a message that mentioned the bot, or a slash-command interaction. A
scheduled task has neither: it fires on a cron with nobody in the channel.

So delivery here is the same shape the scheduler already uses for Telegram:
call the platform's send API directly with the bot token, from whichever
process is doing the sending. No round trip through the bot process, which
may be on another host and holds no route for "post this somewhere".
"""

from __future__ import annotations

import json
import logging
import os

import aiohttp

log = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"

# Discord rejects a message body over 2000 characters outright. The
# briefing is prose and regularly runs longer, so it is split the way the
# bot already splits its own replies rather than truncated.
MESSAGE_LIMIT = 2000


def bot_token() -> str:
    """Discord bot token — keyring first, environment second.

    Same resolution order as `bots/discord_bot.py` so a process delivering
    on the bot's behalf authenticates as the same bot.
    """
    try:
        import keyring  # noqa: PLC0415

        token = keyring.get_password("hive-mind", "DISCORD_BOT_TOKEN")
        if token:
            return token
    except Exception:
        pass
    return os.environ.get("DISCORD_BOT_TOKEN", "")


def split_message(text: str) -> list[str]:
    """Split text into chunks Discord will accept, preserving order."""
    if not text:
        return [""]
    return [text[i : i + MESSAGE_LIMIT] for i in range(0, len(text), MESSAGE_LIMIT)]


async def post_to_channel(
    http: aiohttp.ClientSession,
    token: str,
    channel_id: str,
    text: str,
    audio: bytes | None = None,
    audio_filename: str = "briefing.ogg",
) -> None:
    """Post text (and optionally one audio attachment) into a channel.

    Raises on any non-2xx response. A scheduled delivery that silently
    failed would look identical to one nobody has read yet, which is the
    one thing a channel this quiet cannot afford.

    The audio rides on the final chunk so the player renders under the
    whole message rather than in the middle of it.
    """
    headers = {"Authorization": f"Bot {token}"}
    chunks = split_message(text)
    for index, chunk in enumerate(chunks):
        is_last = index == len(chunks) - 1
        url = f"{DISCORD_API}/channels/{channel_id}/messages"
        if is_last and audio:
            form = aiohttp.FormData()
            form.add_field(
                "payload_json",
                json.dumps({"content": chunk, "attachments": [{"id": 0, "filename": audio_filename}]}),
                content_type="application/json",
            )
            form.add_field(
                "files[0]", audio, filename=audio_filename, content_type="audio/ogg"
            )
            request = http.post(url, data=form, headers=headers)
        else:
            request = http.post(url, json={"content": chunk}, headers=headers)
        async with request as resp:
            if resp.status >= 300:
                body = await resp.text()
                raise RuntimeError(
                    f"Discord post to channel {channel_id} failed: "
                    f"HTTP {resp.status}: {body[:500]}"
                )
