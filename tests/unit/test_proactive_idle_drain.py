"""Unit tests for the shared proactive idle-drain and /proactive endpoint.

Covers the cross-container adaptation of proactive delivery (minds/proactive.py):

* the idle drain buffers unsolicited assistant ``text`` blocks while no request
  is in flight, never reads stdout while ``in_flight`` is set or while the
  request reader holds ``stdout_lock``, and exits on EOF;
* the ``GET /proactive`` endpoint drains-and-empties the buffer and enforces
  bearer auth.
"""

import asyncio
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from minds.proactive import extract_assistant_texts, idle_drain, make_proactive_router


# ---------------------------------------------------------------------------
# Fake stream plumbing
# ---------------------------------------------------------------------------
def _assistant_text_event(text: str) -> bytes:
    return (json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }) + "\n").encode()


def _assistant_tool_event() -> bytes:
    return (json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {}},
            {"type": "tool_result", "content": "output"},
        ]},
    }) + "\n").encode()


def _system_event() -> bytes:
    return (json.dumps({"type": "system", "subtype": "init"}) + "\n").encode()


class _FakeStdout:
    """Serves queued byte lines to ``readline``, then blocks (simulates idle)."""

    def __init__(self, lines):
        self._lines = list(lines)
        self._blocked = asyncio.Event()

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        await self._blocked.wait()
        return b""


class _FakeProc:
    def __init__(self, stdout):
        self.stdout = stdout
        self.returncode = None


def _make_session(lines, in_flight=False, chat_id=555):
    return {
        "proc": _FakeProc(_FakeStdout(lines)),
        "model": "sonnet",
        "chat_id": chat_id,
        "in_flight": in_flight,
        "stdout_lock": asyncio.Lock(),
    }


async def _drain_briefly(session, buffer, ticks=0.4):
    task = asyncio.create_task(idle_drain(session, buffer))
    await asyncio.sleep(ticks)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# extract_assistant_texts
# ---------------------------------------------------------------------------
def test_extract_only_returns_text_blocks_from_assistant_events():
    ev = json.loads(_assistant_text_event("hi").decode())
    assert extract_assistant_texts(ev) == ["hi"]
    assert extract_assistant_texts(json.loads(_assistant_tool_event().decode())) == []
    assert extract_assistant_texts(json.loads(_system_event().decode())) == []


# ---------------------------------------------------------------------------
# idle_drain
# ---------------------------------------------------------------------------
async def test_unsolicited_assistant_text_is_buffered():
    buffer: list[dict] = []
    session = _make_session([_assistant_text_event("proactive hello")], chat_id=777)
    await _drain_briefly(session, buffer)
    assert buffer == [{"chat_id": 777, "text": "proactive hello"}]


async def test_output_while_in_flight_is_not_buffered():
    buffer: list[dict] = []
    session = _make_session([_assistant_text_event("solicited")], in_flight=True)
    await _drain_briefly(session, buffer)
    assert buffer == []


async def test_tool_and_non_assistant_events_never_buffered():
    buffer: list[dict] = []
    session = _make_session([
        _assistant_tool_event(),
        _system_event(),
        (json.dumps({"type": "result", "session_id": "abc"}) + "\n").encode(),
    ])
    await _drain_briefly(session, buffer)
    assert buffer == []


async def test_multiple_text_blocks_each_buffered():
    buffer: list[dict] = []
    session = _make_session([
        (json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "one"},
                {"type": "tool_use", "name": "Bash", "input": {}},
                {"type": "text", "text": "two"},
            ]},
        }) + "\n").encode(),
    ], chat_id=42)
    await _drain_briefly(session, buffer)
    assert buffer == [{"chat_id": 42, "text": "one"}, {"chat_id": 42, "text": "two"}]


async def test_empty_buffer_when_no_output():
    buffer: list[dict] = []
    session = _make_session([])  # stdout blocks forever (idle, silent subprocess)
    await _drain_briefly(session, buffer, ticks=0.3)
    assert buffer == []


async def test_no_chat_id_means_no_buffering():
    buffer: list[dict] = []
    session = _make_session([_assistant_text_event("orphan")], chat_id=None)
    await _drain_briefly(session, buffer)
    assert buffer == []


async def test_drain_does_not_read_stdout_while_request_holds_lock():
    """The idle drain must not consume stdout while the request reader owns the lock."""
    buffer: list[dict] = []
    session = _make_session([_assistant_text_event("should wait")])
    await session["stdout_lock"].acquire()  # simulate live request reader holding lock
    try:
        task = asyncio.create_task(idle_drain(session, buffer))
        await asyncio.sleep(0.3)
        assert buffer == []  # lock held — drain read nothing
    finally:
        session["stdout_lock"].release()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_drain_exits_on_eof():
    """When readline returns empty bytes (EOF), the drain task exits on its own."""
    buffer: list[dict] = []

    class _EofStdout:
        async def readline(self):
            return b""

    session = {
        "proc": _FakeProc(_EofStdout()),
        "chat_id": 1,
        "in_flight": False,
        "stdout_lock": asyncio.Lock(),
    }
    await asyncio.wait_for(idle_drain(session, buffer), timeout=2.0)
    assert buffer == []


async def test_drain_returns_immediately_when_no_stdout():
    buffer: list[dict] = []
    session = {"proc": None, "chat_id": 1, "in_flight": False, "stdout_lock": asyncio.Lock()}
    await asyncio.wait_for(idle_drain(session, buffer), timeout=1.0)
    assert buffer == []


# ---------------------------------------------------------------------------
# /proactive endpoint
# ---------------------------------------------------------------------------
def _client(buffer, token):
    app = FastAPI()
    app.include_router(make_proactive_router(buffer, token))
    return TestClient(app)


def test_endpoint_drains_and_empties_buffer():
    buffer = [{"chat_id": 1, "text": "a"}, {"chat_id": 2, "text": "b"}]
    client = _client(buffer, token=None)
    resp = client.get("/proactive")
    assert resp.status_code == 200
    assert resp.json() == [{"chat_id": 1, "text": "a"}, {"chat_id": 2, "text": "b"}]
    # Buffer emptied — a second poll returns nothing.
    assert buffer == []
    assert client.get("/proactive").json() == []


def test_endpoint_requires_bearer_when_token_set():
    buffer = [{"chat_id": 1, "text": "secret"}]
    client = _client(buffer, token="sekret")
    # No auth header -> 401, buffer untouched.
    assert client.get("/proactive").status_code == 401
    assert buffer == [{"chat_id": 1, "text": "secret"}]
    # Wrong token -> 401.
    assert client.get("/proactive", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert buffer == [{"chat_id": 1, "text": "secret"}]
    # Correct token -> 200 and drains.
    ok = client.get("/proactive", headers={"Authorization": "Bearer sekret"})
    assert ok.status_code == 200
    assert ok.json() == [{"chat_id": 1, "text": "secret"}]
    assert buffer == []


def test_endpoint_empty_buffer_returns_empty_list():
    client = _client([], token=None)
    resp = client.get("/proactive")
    assert resp.status_code == 200
    assert resp.json() == []
