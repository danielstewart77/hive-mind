"""The multi-mind group-chat client shows reasoning under the same rule.

`core/gateway_client.py` is a separate file from the one `hive-surfaces` ships,
not an import of it, and its consumer is the Telegram group-chat bot. A rule
only written in the other copy would leave a dsh or codex mind answering in a
group chat with its reasoning going to nobody — so the rule is tested where it
lives, in both places.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.gateway_client import GatewayClient


def _sse(*events: dict) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


def _thinking(text: str) -> dict:
    return {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "thinking_delta", "thinking": text}}}


def _text(text: str) -> dict:
    return {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "text_delta", "text": text}}}


def _signature() -> dict:
    """What a provider that encrypts its reasoning sends instead."""
    return {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "signature_delta", "signature": "EqoBCkYIBBgCKkBc9"}}}


@pytest.fixture()
def gateway(monkeypatch):
    monkeypatch.delenv("COMMS_BEARER_TOKEN", raising=False)
    client = GatewayClient(
        http=MagicMock(),
        server_url="http://localhost:8420",
        owner_type="telegram:group",
        mind_id="cypher",
    )
    client.ensure_session = AsyncMock(return_value="sess-1")
    return client


def _serve(gateway, body: bytes) -> None:
    async def iter_any():
        for i in range(0, len(body), 11):
            yield body[i:i + 11]

    resp = MagicMock()
    resp.status = 200
    resp.content.iter_any = iter_any
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    gateway.http.post = MagicMock(return_value=ctx)


async def _collect(gateway) -> str:
    return "".join([piece async for piece in gateway.query_stream(1, 2, "hi")])


@pytest.mark.asyncio
async def test_readable_reasoning_reaches_the_group_chat_labelled(gateway):
    _serve(gateway, _sse(_thinking("weighing "), _thinking("it"), _text("the answer")))

    assert await _collect(gateway) == "(thinking) weighing it\n\nthe answer"


@pytest.mark.asyncio
async def test_reasoning_that_carries_no_text_is_not_announced(gateway):
    _serve(gateway, _sse(_signature(), _text("the answer")))

    assert await _collect(gateway) == "the answer"


@pytest.mark.asyncio
async def test_a_streamed_answer_is_not_repeated_by_its_buffered_copy(gateway):
    _serve(gateway, _sse(
        _text("the answer"),
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "the answer"}]}},
    ))

    assert await _collect(gateway) == "the answer"
