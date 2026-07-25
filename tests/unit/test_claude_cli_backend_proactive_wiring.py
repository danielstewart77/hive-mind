"""Integration-ish tests for the claude_cli harness's proactive wiring.

Exercises the real ``minds.harness.claude_cli`` FastAPI app (loaded with the
tracked ``minds/example`` config when ``MIND_NAME`` is unset):

* ``GET /proactive`` is mounted and drains the module buffer;
* ``create_session`` wires ``chat_id`` (from ``client_ref``), a ``stdout_lock``,
  ``in_flight`` and starts an idle-drain task;
* the drain buffers unsolicited assistant text that then surfaces via
  ``/proactive``;
* ``kill_session`` cancels the drain task.

The claude_cli harness owns a long-lived subprocess with a drainable stdout —
this is the wiring the codex harness deliberately lacks (per-turn spawn).
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


class _FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)
        self._blocked = asyncio.Event()

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        await self._blocked.wait()
        return b""


class _FakeProc:
    def __init__(self, lines):
        self.stdout = _FakeStdout(lines)
        self.stdin = None
        self.returncode = None
        self.pid = 4321

    def send_signal(self, sig):  # pragma: no cover - not exercised here
        pass


def _assistant_text(text: str) -> bytes:
    return (json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }) + "\n").encode()


@pytest.fixture()
def backend():
    from minds.harness import claude_cli as impl
    impl.SESSIONS.clear()
    impl.PROACTIVE_BUFFER.clear()
    yield impl
    impl.SESSIONS.clear()
    impl.PROACTIVE_BUFFER.clear()


def test_proactive_endpoint_mounted_and_drains(backend):
    backend.PROACTIVE_BUFFER.append({"chat_id": 9, "text": "hi"})
    client = TestClient(backend.app)
    # The router closes over the token captured at import time (PROACTIVE_TOKEN,
    # from COMMS_BEARER_TOKEN). Present it when set; omit it when unset.
    headers = (
        {"Authorization": f"Bearer {backend.PROACTIVE_TOKEN}"}
        if backend.PROACTIVE_TOKEN else {}
    )
    resp = client.get("/proactive", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == [{"chat_id": 9, "text": "hi"}]
    assert backend.PROACTIVE_BUFFER == []


async def test_create_session_wires_drain_and_buffers_unsolicited_text(backend):
    fake = _FakeProc([_assistant_text("unsolicited text")])

    with patch.object(backend, "_spawn_proc", new=AsyncMock(return_value=fake)):
        resp = await backend.create_session(_JsonRequest({
            "session_id": "s1",
            "client_ref": "12345",
        }))

    assert resp["status"] == "running"
    sess = backend.SESSIONS["s1"]
    assert sess["chat_id"] == "12345"
    assert isinstance(sess["stdout_lock"], asyncio.Lock)
    assert sess["in_flight"] is False
    assert "drain_task" in sess

    # Let the drain run a few ticks and pick up the buffered assistant text.
    for _ in range(20):
        await asyncio.sleep(0.05)
        if backend.PROACTIVE_BUFFER:
            break
    assert backend.PROACTIVE_BUFFER == [{"chat_id": 12345, "text": "unsolicited text"}]

    sess["drain_task"].cancel()
    try:
        await sess["drain_task"]
    except asyncio.CancelledError:
        pass


async def test_kill_session_cancels_drain(backend):
    async def _never():
        await asyncio.Event().wait()

    drain_task = asyncio.ensure_future(_never())
    backend.SESSIONS["kill-me"] = {
        "proc": _FakeProc([]),
        "model": "sonnet",
        "drain_task": drain_task,
    }
    with patch.object(backend, "_kill_proc", new=AsyncMock()):
        resp = await backend.kill_session("kill-me")
    assert resp["status"] == "closed"
    assert drain_task.cancelled() or drain_task.cancelling() > 0
    drain_task.cancel()
    try:
        await drain_task
    except asyncio.CancelledError:
        pass


class _JsonRequest:
    """Minimal stand-in for starlette Request exposing ``.json()``."""

    def __init__(self, body: dict):
        self._body = body

    async def json(self):
        return self._body
