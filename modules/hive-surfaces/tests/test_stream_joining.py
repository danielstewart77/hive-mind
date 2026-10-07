"""A streamed reply reaches a surface as the text the mind actually wrote.

The gateway yields two granularities on one channel: per-token `text_delta`
fragments (which split mid-word) and whole buffered `assistant` blocks. A
caller cannot tell them apart, so `query_stream` emits the break between
blocks itself and every caller concatenates plainly.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from hive_surfaces.discord_bot import _stream_to_message
from hive_surfaces.gateway_client import GatewayClient


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


def _block_stop(index: int) -> dict:
    return {"type": "stream_event",
            "event": {"type": "content_block_stop", "index": index}}


def _by_delegate(event: dict, tool_use_id: str = "toolu_1") -> dict:
    return {**event, "parent_tool_use_id": tool_use_id}


@pytest.fixture()
def gateway(monkeypatch):
    monkeypatch.delenv("COMMS_BEARER_TOKEN", raising=False)
    client = GatewayClient(
        http=MagicMock(),
        server_url="http://localhost:8420",
        owner_type="discord:skippy",
        mind_id="skippy",
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
    async def test_a_delegates_turn_is_not_relayed_as_the_minds_own(
        self, gateway,
    ):
        """R1: a sub-agent's prose is not part of the reply the mind wrote."""
        _serve(gateway, _sse(
            _block_start(0), _delta(0, "I'll have a subagent look."),
            _by_delegate(_block_start(0)),
            _by_delegate(_delta(0, "Searching the repo now...")),
            _by_delegate(_assistant("I found three call sites.")),
            _block_start(1), _delta(1, "Here's the summary."),
        ))

        assert "".join(await _collect(gateway)) == (
            "I'll have a subagent look.\n\nHere's the summary."
        )

    @pytest.mark.asyncio
    async def test_blocks_stay_separated_when_only_stop_frames_arrive(
        self, gateway,
    ):
        """R2: the break survives an upstream that forwards only block ends."""
        _serve(gateway, _sse(
            _delta(0, "First."), _block_stop(0),
            _delta(0, "Second."), _block_stop(0),
        ))

        assert "".join(await _collect(gateway)) == "First.\n\nSecond."

    @pytest.mark.asyncio
    async def test_a_block_ending_in_a_newline_still_gets_one_blank_line(
        self, gateway,
    ):
        """R2: the break is one blank line, not one plus whatever text carried."""
        _serve(gateway, _sse(_assistant("First paragraph.\n", "Second one.")))

        assert "".join(await _collect(gateway)) == (
            "First paragraph.\n\nSecond one."
        )

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

        monkeypatch.setattr("hive_surfaces.discord_bot.gateway",
                            SimpleNamespace(query_stream=stream))
        sent = MagicMock()
        sent.edit = AsyncMock()
        sent.channel.send = AsyncMock()

        result = await _stream_to_message(sent, 1, 2, "hi")

        assert result == "I'm looking into the bot's code"
        assert sent.edit.await_args.kwargs["content"] == "I'm looking into the bot's code"


def _thinking_delta(index: int, text: str) -> dict:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "thinking_delta", "thinking": text},
        },
    }


def _redacted_thinking_delta(index: int) -> dict:
    """What a provider that encrypts its reasoning sends: no readable text."""
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "signature_delta", "signature": "EqoBCkYIBBgCKkBc9"},
        },
    }


class TestReasoningIsShownOnlyWhenItIsReadable:

    @pytest.mark.asyncio
    async def test_readable_reasoning_reaches_the_surface_labelled(self, gateway):
        """Reasoning the provider sends as text is shown, told apart from the answer."""
        _serve(gateway, _sse(
            _block_start(0), _thinking_delta(0, "weighing "), _thinking_delta(0, "it"),
            _block_start(1), _delta(1, "the answer"),
        ))

        assert "".join(await _collect(gateway)) == (
            "(thinking) weighing it\n\nthe answer"
        )

    @pytest.mark.asyncio
    async def test_reasoning_that_carries_no_text_is_never_announced(self, gateway):
        """An encrypted or redacted thinking block yields nothing, not an empty label."""
        _serve(gateway, _sse(
            _block_start(0), _redacted_thinking_delta(0),
            _block_start(1), _delta(1, "the answer"),
        ))

        assert "".join(await _collect(gateway)) == "the answer"

    @pytest.mark.asyncio
    async def test_buffered_reasoning_is_shown_and_an_encrypted_one_is_not(
        self, gateway,
    ):
        """A harness that buffers instead of streaming obeys the same rule."""
        _serve(gateway, _sse(_assistant_blocks(
            {"type": "redacted_thinking", "data": "EroBCkYIBBgCKkBc9"},
            {"type": "thinking", "thinking": "weighing it"},
            {"type": "text", "text": "the answer"},
        )))

        assert "".join(await _collect(gateway)) == (
            "(thinking) weighing it\n\nthe answer"
        )

    @pytest.mark.asyncio
    async def test_streamed_reasoning_is_not_repeated_by_the_buffered_copy(
        self, gateway,
    ):
        """The deltas and the block at the end are the same reasoning once."""
        _serve(gateway, _sse(
            _block_start(0), _thinking_delta(0, "weighing it"),
            _block_start(1), _delta(1, "the answer"),
            _assistant_blocks(
                {"type": "thinking", "thinking": "weighing it"},
                {"type": "text", "text": "the answer"},
            ),
        ))

        assert "".join(await _collect(gateway)) == (
            "(thinking) weighing it\n\nthe answer"
        )


def _dsh_delta(kind: str, text: str) -> dict:
    """A delta shaped the way the dsh adapter emits one.

    It differs from the claude path in the two ways that matter here: every
    delta claims block index 0, and no `content_block_start` ever arrives. So
    the epoch never moves and the index never changes, and the kind is the only
    thing that can separate reasoning from the answer.
    """
    inner = ({"type": "thinking_delta", "thinking": text} if kind == "reasoning"
             else {"type": "text_delta", "text": text})
    return {"type": "stream_event",
            "event": {"type": "content_block_delta", "index": 0, "delta": inner}}


class TestReasoningAndAnswerStaySeparate:

    @pytest.mark.asyncio
    async def test_the_dsh_shape_still_breaks_between_reasoning_and_answer(
        self, gateway,
    ):
        """A harness that numbers every delta zero still gets the break."""
        _serve(gateway, _sse(
            _dsh_delta("reasoning", "weighing "), _dsh_delta("reasoning", "it"),
            _dsh_delta("text", "the "), _dsh_delta("text", "answer"),
        ))

        assert "".join(await _collect(gateway)) == (
            "(thinking) weighing it\n\nthe answer"
        )

    @pytest.mark.asyncio
    async def test_reasoning_resumed_after_an_answer_is_labelled_again(
        self, gateway,
    ):
        """A second run of reasoning says so rather than reading as more answer."""
        _serve(gateway, _sse(
            _dsh_delta("text", "first thought"),
            _dsh_delta("reasoning", "reconsidering"),
            _dsh_delta("text", "second thought"),
        ))

        assert "".join(await _collect(gateway)) == (
            "first thought\n\n(thinking) reconsidering\n\nsecond thought"
        )


class TestWhatStreamedAndWhatDidNotAreJudgedApart:

    @pytest.mark.asyncio
    async def test_a_buffered_answer_survives_reasoning_that_streamed(
        self, gateway,
    ):
        """Reasoning arriving as deltas must not suppress an answer that did not."""
        _serve(gateway, _sse(
            _dsh_delta("reasoning", "weighing it"),
            _assistant_blocks({"type": "text", "text": "the answer"}),
        ))

        assert "".join(await _collect(gateway)) == (
            "(thinking) weighing it\n\nthe answer"
        )

    @pytest.mark.asyncio
    async def test_reasoning_that_would_land_after_the_answer_is_dropped(
        self, gateway,
    ):
        """Thinking leads or it is not shown — never appended to its own conclusion."""
        _serve(gateway, _sse(
            _dsh_delta("text", "the answer"),
            _assistant_blocks({"type": "thinking", "thinking": "weighing it"}),
        ))

        assert "".join(await _collect(gateway)) == "the answer"
