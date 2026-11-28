"""Command replies survive Telegram's 4096-character limit, and network blips.

A command whose reply ran past the limit came back from Telegram as a
BadRequest, which the bot's error handler logs as a transient network blip and
drops. Nothing reached the chat, so the command read as broken rather than as
too chatty — /sessions spent months looking dead for exactly this reason. A
`NetworkError` does the same thing, so command replies now take the same
guaranteed-delivery path a tapped button does.
"""
import asyncio

from hive_surfaces.telegram_bot import _chunk_message, _reply_chunked


class _Bot:
    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, chat_id, text):
        assert len(text) <= 4096, "chunk handed to Telegram is over the limit"
        self.sent.append(text)


class _Update:
    def __init__(self):
        self.bot = _Bot()
        self.effective_chat = type("_Chat", (), {"id": 456})()

    def get_bot(self):
        return self.bot


def test_short_reply_is_one_message():
    update = _Update()
    asyncio.run(_reply_chunked(update, "No sessions found."))

    assert update.bot.sent == ["No sessions found."]


def test_oversized_reply_is_split_and_fully_delivered():
    body = "".join(f"{i}. session line\n" for i in range(600))
    assert len(body) > 4096

    update = _Update()
    asyncio.run(_reply_chunked(update, body))

    assert len(update.bot.sent) == len(_chunk_message(body)) > 1
    assert "".join(update.bot.sent) == body
