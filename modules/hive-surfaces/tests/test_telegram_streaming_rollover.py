"""A streamed reply that outgrows one Telegram message keeps moving.

The preview was always chunk zero of the accumulated text, so past the 4096
limit it stopped changing: every later edit was byte-identical, Telegram refused
it as unmodified, and the refusal was swallowed. On screen that is a reply that
freezes partway through — indistinguishable from the wedged mind the streaming
was added to rule out. Cypher's goal-driven builds stream for forty minutes and
cross that limit in the first few.
"""

import asyncio

import pytest

from hive_surfaces import telegram_bot
from hive_surfaces.telegram_bot import TELEGRAM_MSG_LIMIT, _chunk_message, _stream_to_message


class _Message:
    """A sent Telegram message that records every edit and every reply to it."""

    def __init__(self, text: str, chat: "_Chat") -> None:
        self.chat = chat
        self.edits: list[str] = [text]
        chat.messages.append(self)

    async def edit_text(self, text: str) -> None:
        if text == self.edits[-1]:
            raise RuntimeError("Message is not modified")
        assert len(text) <= TELEGRAM_MSG_LIMIT, "edit handed to Telegram is over the limit"
        self.edits.append(text)

    async def reply_text(self, text: str) -> "_Message":
        assert len(text) <= TELEGRAM_MSG_LIMIT, "message handed to Telegram is over the limit"
        return _Message(text, self.chat)

    @property
    def text(self) -> str:
        return self.edits[-1]


class _Chat:
    def __init__(self) -> None:
        self.messages: list[_Message] = []


@pytest.fixture()
def streamed(monkeypatch):
    """Drive `_stream_to_message` over a scripted stream, with no clock wait."""
    def drive(pieces: list[str]) -> _Chat:
        async def query_stream(*_args, **_kwargs):
            for piece in pieces:
                yield piece

        monkeypatch.setattr(
            telegram_bot, "gateway",
            type("_Gateway", (), {"query_stream": staticmethod(query_stream)})(),
        )
        monkeypatch.setattr(telegram_bot, "_sanitize_response", lambda text: text)
        chat = _Chat()
        placeholder = _Message("…", chat)
        asyncio.run(_stream_to_message(
            placeholder, 1, 2, "go", edit_interval=0.0,
        ))
        return chat

    return drive


def test_an_answer_past_the_limit_rolls_into_a_second_message(streamed):
    body = "".join(f"paragraph {i} of the answer\n" for i in range(400))
    assert len(body) > TELEGRAM_MSG_LIMIT
    expected = _chunk_message(body)
    assert len(expected) > 1

    chat = streamed([f"paragraph {i} of the answer\n" for i in range(400)])

    assert [message.text for message in chat.messages] == expected


def test_the_newest_message_keeps_updating_after_the_rollover(streamed):
    body = "".join(f"paragraph {i} of the answer\n" for i in range(400))
    assert len(_chunk_message(body)) > 1

    chat = streamed([f"paragraph {i} of the answer\n" for i in range(400)])

    # The last message was edited repeatedly as the tail grew, rather than
    # written once at the end — that movement is the whole point.
    assert len(chat.messages[-1].edits) > 2


def test_a_reply_inside_one_message_still_uses_that_message(streamed):
    chat = streamed(["short ", "answer"])

    assert len(chat.messages) == 1
    assert chat.messages[0].text == "short answer"
