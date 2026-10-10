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


@pytest.mark.asyncio
async def test_codex_reasoning_reaches_the_surface_as_readable_thinking():
    """Codex's reasoning is plain text, so it travels under the same rule as dsh's.

    Before this it was captured only to compose the empty-turn diagnostic, so a
    turn that reasoned at length and then answered showed the operator none of
    the thinking — on a harness whose reasoning is perfectly readable.
    """
    from minds.harness import codex_cli as codex_impl

    codex_impl.SESSIONS.clear()
    codex_impl.SESSIONS["sess-2"] = {
        "system_prompt": "system",
        "thread_id": None,
        "model": "gpt-5",
    }

    lines = [
        json.dumps({"type": "thread.started", "thread_id": "thread-9"}).encode() + b"\n",
        json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_reasoning", "text": "weighing it"},
        }).encode() + b"\n",
        json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "the answer"},
        }).encode() + b"\n",
        json.dumps({"type": "turn.completed"}).encode() + b"\n",
    ]

    with patch("minds.harness.codex_cli.asyncio.create_subprocess_exec",
               return_value=_FakeProcess(lines)):
        events = [event async for event in codex_impl._run_codex_turn("sess-2", "hi", None)]

    thinking = [
        event["event"]["delta"]["thinking"]
        for event in events
        if event.get("type") == "stream_event"
        and event["event"].get("type") == "content_block_delta"
        and event["event"]["delta"].get("type") == "thinking_delta"
    ]
    assert thinking == ["weighing it"]
    # Ahead of the answer, so the operator reads the deliberation first and a
    # voice surface speaks it in that order.
    kinds = [event.get("type") for event in events]
    assert kinds.index("stream_event") < kinds.index("assistant")


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_id", [None, "thread-9"])   # first turn, and every later one
@pytest.mark.parametrize("effort,expected", [
    ("high", ['-c', 'model_reasoning_effort="high"']),
    (None, []),
])
async def test_a_chat_turn_runs_at_the_conversations_effort(effort, expected, thread_id):
    """Unset passes nothing, leaving the profile's own level in force."""
    from minds.harness import codex_cli as codex_impl

    codex_impl.SESSIONS.clear()
    codex_impl.SESSIONS["sess-1"] = {
        "system_prompt": "system", "thread_id": thread_id, "model": "gpt-5", "effort": effort,
    }
    lines = [json.dumps({"type": "turn.completed"}).encode() + b"\n"]

    with patch("minds.harness.codex_cli.asyncio.create_subprocess_exec",
               return_value=_FakeProcess(lines)) as spawned:
        [event async for event in codex_impl._run_codex_turn("sess-1", "hello", None)]

    cmd = list(spawned.call_args.args)
    reasoning = [a for a in cmd if "model_reasoning_effort" in a]
    assert (["-c", reasoning[0]] if reasoning else []) == expected
    codex_impl.SESSIONS.clear()
