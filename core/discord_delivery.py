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

import asyncio
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

# Discord allows 5 messages per 5 seconds per channel. A long briefing is
# several chunks back to back, so a 429 partway through is ordinary rather
# than exceptional — and raising on it leaves a briefing that stops
# mid-sentence in the channel with no marker.
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_FALLBACK_WAIT_S = 1.0


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
) -> None:
    """Post text into a channel, split across as many messages as it needs.

    Raises on any non-2xx response. A scheduled delivery that silently
    failed would look identical to one nobody has read yet, which is the
    one thing a channel this quiet cannot afford.
    """
    headers = {"Authorization": f"Bot {token}"}
    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    for chunk in split_message(text):
        await _post_with_retry(
            http, url, headers, channel_id, json_body={"content": chunk}
        )


async def post_audio(
    http: aiohttp.ClientSession,
    token: str,
    channel_id: str,
    audio: bytes,
    filename: str = "briefing.ogg",
) -> None:
    """Post one audio attachment into a channel as its own message.

    Deliberately not folded into `post_to_channel`. Attaching a file is a
    separate per-channel permission and an ogg can be refused on size, so a
    refusal here must cost the audio and nothing else — sending both on one
    request means a channel without Attach Files receives no briefing at
    all.
    """
    headers = {"Authorization": f"Bot {token}"}
    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    form = aiohttp.FormData()
    form.add_field(
        "payload_json",
        json.dumps({"attachments": [{"id": 0, "filename": filename}]}),
        content_type="application/json",
    )
    form.add_field("files[0]", audio, filename=filename, content_type="audio/ogg")
    async with http.post(url, data=form, headers=headers) as resp:
        await _raise_for_status(resp, channel_id)


async def _post_with_retry(http, url, headers, channel_id, json_body) -> None:
    """POST one message, waiting out a rate limit rather than failing on it."""
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        async with http.post(url, json=json_body, headers=headers) as resp:
            if resp.status != 429 or attempt == RATE_LIMIT_RETRIES:
                await _raise_for_status(resp, channel_id)
                return
            wait = RATE_LIMIT_FALLBACK_WAIT_S
            try:
                body = await resp.json()
                wait = float(body.get("retry_after", wait))
            except Exception:
                pass
        log.warning(
            "Rate limited posting to channel %s; retrying in %.2fs", channel_id, wait
        )
        await asyncio.sleep(wait)


async def _raise_for_status(resp, channel_id: str) -> None:
    if resp.status >= 300:
        body = await resp.text()
        raise RuntimeError(
            f"Discord post to channel {channel_id} failed: "
            f"HTTP {resp.status}: {body[:500]}"
        )
