"""A streamed reply reaches a surface as the text the mind actually wrote.

The gateway yields two granularities on one channel: per-token `text_delta`
fragments (which split mid-word) and whole buffered `assistant` blocks. A
caller cannot tell them apart, so `query_stream` emits the break between
blocks itself and every caller concatenates plainly.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bots.discord_bot import _stream_to_message
from core.gateway_client import GatewayClient


def _sse(*events: dict) -> bytes:
    """Encode events as an SSE body the way hive-comms writes one."""
    import json
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


def _delta(index: int, text: str) -> dict:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        },
    }


def _block_start(index: int) -> dict:
    return {"type": "stream_event",
            "event": {"type": "content_block_start", "index": index}}


def _assistant(*texts: str) -> dict:
    return {"type": "assistant",
            "message": {"content": [{"type": "text", "text": t} for t in texts]}}


def _assistant_blocks(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks)}}


def _message_start() -> dict:
    return {"type": "stream_event", "event": {"type": "message_start"}}


@pytest.fixture()
def gateway(monkeypatch):
    monkeypatch.delenv("COMMS_BEARER_TOKEN", raising=False)
    client = GatewayClient(
        http=MagicMock(),
        server_url="http://localhost:8420",
        owner_type="discord:ada",
        mind_id="ada",
    )
    client.ensure_session = AsyncMock(return_value="sess-1")
    return client


def _serve(gateway, body: bytes, packet: int = 7) -> None:
    """Feed `body` to the client in small packets, as a socket would."""
    async def iter_any():
        for i in range(0, len(body), packet):
            yield body[i:i + packet]

    resp = MagicMock()
    resp.status = 200
    resp.content.iter_any = iter_any
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    gateway.http.post = MagicMock(return_value=ctx)


async def _collect(gateway) -> list[str]:
    return [piece async for piece in gateway.query_stream(1, 2, "hi")]


class TestStreamCarriesTheMindsText:

    @pytest.mark.asyncio
    async def test_token_deltas_within_one_block_join_into_the_original_text(
        self, gateway,
    ):
        """R1: deltas that split mid-word rejoin into exactly what was written."""
        _serve(gateway, _sse(
            _block_start(0),
            _delta(0, "I"), _delta(0, "'"), _delta(0, "m looking into"),
            _delta(0, " the b"), _delta(0, "ot's code"),
        ))

        assert "".join(await _collect(gateway)) == "I'm looking into the bot's code"

    @pytest.mark.asyncio
    async def test_a_new_block_is_separated_by_one_blank_line(self, gateway):
        """R2: the break the mind put between two blocks survives, exactly once."""
        _serve(gateway, _sse(
            _block_start(0), _delta(0, "First para"), _delta(0, "graph."),
            _block_start(1), _delta(1, "Second one."),
        ))

        assert "".join(await _collect(gateway)) == "First paragraph.\n\nSecond one."

    @pytest.mark.asyncio
    async def test_buffered_blocks_are_separated_the_same_way(self, gateway):
        """R2: a mind that buffers instead of streaming gets the same breaks."""
        _serve(gateway, _sse(_assistant("First paragraph.", "Second one.")))

        assert "".join(await _collect(gateway)) == "First paragraph.\n\nSecond one."

    @pytest.mark.asyncio
    async def test_two_messages_reusing_block_index_zero_stay_separated(
        self, gateway,
    ):
        """R2: a block index repeats across messages; the break still lands."""
        _serve(gateway, _sse(
            _message_start(), _block_start(0), _delta(0, "First."),
            _message_start(), _block_start(0), _delta(0, "Second."),
        ))

        assert "".join(await _collect(gateway)) == "First.\n\nSecond."

    @pytest.mark.asyncio
    async def test_a_block_already_streamed_as_deltas_is_not_repeated(
        self, gateway,
    ):
        """R1: a mind that streams and then buffers the same text sends it once."""
        _serve(gateway, _sse(
            _block_start(0), _delta(0, "Half a "), _delta(0, "sentence."),
            _assistant("Half a sentence."),
        ))

        assert "".join(await _collect(gateway)) == "Half a sentence."

    @pytest.mark.asyncio
    async def test_only_text_blocks_reach_the_surface(self, gateway):
        """R1: a tool_use block carrying a text key is not part of the reply."""
        _serve(gateway, _sse(_assistant_blocks(
            {"type": "tool_use", "name": "Bash", "text": "rm -rf /"},
            {"type": "text", "text": "Done."},
        )))

        assert "".join(await _collect(gateway)) == "Done."

    @pytest.mark.asyncio
    async def test_query_returns_the_stream_concatenated(self, gateway):
        """R1: the non-streaming helper joins the same way its stream demands."""
        _serve(gateway, _sse(
            _block_start(0), _delta(0, "Half a "), _delta(0, "sentence."),
        ))

        assert await gateway.query(1, 2, "hi") == "Half a sentence."


class TestDiscordPostsWhatItWasStreamed:

    @pytest.mark.asyncio
    async def test_discord_posts_the_pieces_without_inserting_breaks(
        self, monkeypatch,
    ):
        """R1: the Discord surface concatenates plainly, mid-word splits included."""
        pieces = ["I", "'", "m looking into", " the b", "ot's code"]

        async def stream(*_a, **_k):
            for piece in pieces:
                yield piece

        monkeypatch.setattr("bots.discord_bot.gateway",
                            SimpleNamespace(query_stream=stream))
        sent = MagicMock()
        sent.edit = AsyncMock()
        sent.channel.send = AsyncMock()

        result = await _stream_to_message(sent, 1, 2, "hi")

        assert result == "I'm looking into the bot's code"
        assert sent.edit.await_args.kwargs["content"] == "I'm looking into the bot's code"
