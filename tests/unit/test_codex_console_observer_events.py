"""Tests for the codex harness's observer-only Codex events."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest


class _AsyncLineReader:
    def __init__(self, lines):
        self._lines = iter(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._lines)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeProcess:
    def __init__(self, lines):
        self.stdout = _AsyncLineReader(lines)
        self.stderr = _AsyncLineReader([])
        # Already exited by the time the relay reaps it — _reap_proc treats a
        # set returncode as nothing-to-kill.
        self.returncode = 0
        self.pid = 4321
        self.stdin = type(
            "_FakeStdin",
            (),
            {
                "write": lambda self, data: len(data),
                "drain": AsyncMock(),
                "close": lambda self: None,
            },
        )()

    async def wait(self):
        return 0


@pytest.mark.asyncio
async def test_codex_send_emits_observer_only_codex_events():
    from minds.harness import codex_cli as codex_impl

    codex_impl.SESSIONS.clear()
    codex_impl.SESSIONS["sess-1"] = {
        "system_prompt": "system",
        "thread_id": None,
        "model": "gpt-5",
    }

    lines = [
        json.dumps({"type": "thread.started", "thread_id": "thread-123"}).encode() + b"\n",
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "hello from codex"},
            }
        ).encode() + b"\n",
        json.dumps({"type": "turn.completed"}).encode() + b"\n",
    ]

    with patch("minds.harness.codex_cli.asyncio.create_subprocess_exec", return_value=_FakeProcess(lines)):
        events = [event async for event in codex_impl._run_codex_turn("sess-1", "hello", None)]

    codex_events = [event for event in events if event["type"] == "codex_event"]
    assert len(codex_events) == 3
    assert all(event["_observer_only"] is True for event in codex_events)
    assert codex_events[0]["event"]["type"] == "thread.started"
    assert codex_events[1]["event"]["type"] == "item.completed"
    assert codex_events[2]["event"]["type"] == "turn.completed"

    assistant_events = [event for event in events if event["type"] == "assistant"]
    assert len(assistant_events) == 1
    assert assistant_events[0]["message"]["content"][0]["text"] == "hello from codex"

    assert events[-1]["type"] == "result"
    assert events[-1]["session_id"] == "thread-123"

    codex_impl.SESSIONS.clear()
