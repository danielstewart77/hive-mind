"""Posting a mind's answer into a Discord channel, from the gateway.

`core/discord_delivery.py` does this same job for the scheduler, and this is
deliberately not an import of it: comms builds from `./nervous-system` as its
own context and cannot see the repository root, so the alternative to these
few dozen lines is widening the gateway's build to the whole tree.

Kept narrow on purpose — one POST, no bot process, no session. An escalation
has to reach the channel whether or not any bot is up, which is the same
reason `notify.py` talks to the REST API directly rather than hopping
through a running surface.
"""

from __future__ import annotations

import logging
import os

import aiohttp

log = logging.getLogger("hive-mind.broker")

DISCORD_API = "https://discord.com/api/v10"

#: Discord rejects a body over 2000 characters outright. A mind's answer is
#: prose and regularly runs longer, so it is split the way the bot splits its
#: own replies rather than truncated — a briefing that stops mid-sentence
#: reads as the whole answer.
MESSAGE_LIMIT = 2000

#: Discord allows 5 messages per 5 seconds per channel, so a long answer
#: going out as several chunks back to back will meet a 429 as a matter of
#: course. Raising on it would leave the tail of an escalation unsent.
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_FALLBACK_WAIT_S = 1.0

POST_TIMEOUT_S = 15.0


def bot_token() -> str:
    """The Discord bot token, from the gateway's environment.

    Empty rather than raising. The caller's remedy for an unset token is to
    log and carry on: the answer has already reached the mind that asked for
    it, and losing the copy to the channel must not fail the dispatch.
    """
    return (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()


def split_message(text: str) -> list[str]:
    """Split text into chunks Discord will accept, preserving order."""
    if not text:
        return []
    return [text[i:i + MESSAGE_LIMIT] for i in range(0, len(text), MESSAGE_LIMIT)]


class DeliveryFailed(RuntimeError):
    """The channel did not take the message."""


async def post(channel_id: str, text: str, *, token: str | None = None) -> int:
    """Post `text` into `channel_id`, in order. Returns the chunks delivered.

    Raises `DeliveryFailed` when the first chunk is refused, because that is
    a destination or a credential problem and nothing arrived. A later chunk
    failing is logged and the rest still attempted: part of a long answer in
    the channel beats none of it.
    """
    bot = token if token is not None else bot_token()
    if not bot:
        raise DeliveryFailed("DISCORD_BOT_TOKEN is not set in the gateway")

    chunks = split_message(text)
    if not chunks:
        return 0

    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {bot}"}
    timeout = aiohttp.ClientTimeout(total=POST_TIMEOUT_S)
    delivered = 0

    async with aiohttp.ClientSession(timeout=timeout) as http:
        for index, chunk in enumerate(chunks):
            try:
                await _post_chunk(http, url, headers, chunk)
            except Exception as exc:
                if index == 0:
                    raise DeliveryFailed(str(exc)) from exc
                log.warning(
                    "discord_out: chunk %d of %d refused for channel %s: %s",
                    index + 1, len(chunks), channel_id, exc,
                )
                continue
            delivered += 1

    return delivered


async def _post_chunk(http, url: str, headers: dict, content: str) -> None:
    """One chunk, retrying only the rate limit Discord tells us to wait out."""
    import asyncio

    for attempt in range(RATE_LIMIT_RETRIES):
        async with http.post(url, headers=headers,
                             json={"content": content}) as resp:
            if resp.status in (200, 201):
                return
            if resp.status != 429 or attempt == RATE_LIMIT_RETRIES - 1:
                body = (await resp.text())[:200]
                raise RuntimeError(f"Discord returned {resp.status}: {body}")
            wait = RATE_LIMIT_FALLBACK_WAIT_S
            try:
                payload = await resp.json()
                wait = float(payload.get("retry_after") or wait)
            except Exception:
                pass
        await asyncio.sleep(wait)
