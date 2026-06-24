"""Tests for the scheduler's TTS call path.

Verifies that the scheduler threads `voice_id` through `_tts` and
`_try_send_voice`, so the voice server can resolve the correct per-mind
voice reference clip instead of falling back to a literal `"default"`.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bots import scheduler


class _AsyncCtx:
    def __init__(self, ret):
        self._ret = ret

    async def __aenter__(self):
        return self._ret

    async def __aexit__(self, *_):
        return False


@pytest.mark.asyncio
async def test_tts_sends_voice_id_in_payload():
    resp = MagicMock()
    resp.status = 200
    resp.read = AsyncMock(return_value=b"OGGBYTES")

    http = MagicMock()
    http.post = MagicMock(return_value=_AsyncCtx(resp))

    audio = await scheduler._tts(http, "hello", "ada-uuid-1234")

    assert audio == b"OGGBYTES"
    http.post.assert_called_once()
    _, kwargs = http.post.call_args
    assert kwargs["json"] == {"text": "hello", "voice_id": "ada-uuid-1234"}


@pytest.mark.asyncio
async def test_tts_sets_explicit_timeout():
    """The TTS POST must carry an explicit generous timeout so a long
    synthesis queued ahead of it on a busy GPU server can't trip aiohttp's
    silent 300s default."""
    resp = MagicMock()
    resp.status = 200
    resp.read = AsyncMock(return_value=b"OGG")

    http = MagicMock()
    http.post = MagicMock(return_value=_AsyncCtx(resp))

    await scheduler._tts(http, "hello", "ada-uuid-1234")

    _, kwargs = http.post.call_args
    timeout = kwargs["timeout"]
    assert timeout.total == scheduler.VOICE_TTS_TIMEOUT_SECONDS
    assert timeout.total >= 600


@pytest.mark.asyncio
async def test_try_send_voice_threads_voice_id_to_tts():
    with (
        patch.object(scheduler, "_tts", new=AsyncMock(return_value=b"OGG")) as tts_mock,
        patch.object(scheduler, "_send_voice", new=AsyncMock()) as send_mock,
    ):
        await scheduler._try_send_voice(
            bot_token="tok", chat_id=42, text="hi", voice_id="ada-uuid", label="1pm"
        )

    tts_mock.assert_awaited_once()
    args, _ = tts_mock.call_args
    assert args[1] == "hi"
    assert args[2] == "ada-uuid"
    send_mock.assert_awaited_once()
