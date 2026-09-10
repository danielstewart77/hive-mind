"""The gateway authenticates itself to every mind it calls.

Six HTTP call sites and one WebSocket proxy reach a mind's session surface.
Each carries that mind's own credential, looked up per call from the broker
row the mind registered it on. One token taken off one mind opens that mind
and no other, and the token never leaves the gateway through any route.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
from unittest.mock import patch

import aiohttp
import pytest

from comms import broker
from comms.sessions import MindCallFailed, MindRefusedCredential, SessionManager

MIND_A = "11111111-1111-4111-8111-111111111111"
MIND_B = "22222222-2222-4222-8222-222222222222"


def _run(coro):
    return asyncio.run(coro)


async def _broker_db(tmp: str):
    db = await broker.init_db(os.path.join(tmp, "broker.db"))
    await broker.register_mind(
        db, mind_id=MIND_A, name="alpha", gateway_url="http://alpha:8420",
        model="sonnet", harness="claude_cli", session_token="alpha-token",
    )
    await broker.register_mind(
        db, mind_id=MIND_B, name="beta", gateway_url="http://beta:8420",
        model="sonnet", harness="codex_cli", session_token="beta-token",
    )
    return db


async def _manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager()
    await mgr.start()
    mgr.broker_db = await _broker_db(tmp)
    return mgr


async def _seed_session(mgr: SessionManager, mind_id: str = MIND_A) -> str:
    session_id = "sess-auth"
    now = time.time()
    await mgr._db.execute(
        """INSERT INTO sessions (id, owner_type, owner_ref, model, created_at,
                                 last_active, status, mind_id, summary, claude_sid)
           VALUES (?, 'telegram', '123', 'sonnet', ?, ?, 'running', ?, 'chat', 'conv-1')""",
        (session_id, now, now, mind_id),
    )
    await mgr._db.commit()
    mgr._procs[session_id] = {"_mind_url": "http://alpha:8420"}
    mgr._mind_ids[session_id] = mind_id
    return session_id


# ---------------------------------------------------------------------------
# Recording stand-in for aiohttp
# ---------------------------------------------------------------------------
class _Response:
    def __init__(self, status: int = 200, body: str = "{}"):
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def read(self) -> bytes:
        return self._body.encode()

    async def json(self) -> dict:
        return {"released": True, "ok": True}

    @property
    def content(self):
        async def _lines():
            if False:  # pragma: no cover - an empty SSE stream
                yield b""
        return _lines()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _RecordingSession:
    """Records every outbound call's headers. One instance per test via calls."""

    calls: list[dict] = []
    response = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _record(self, method, url, **kwargs):
        type(self).calls.append(
            {"method": method, "url": url, "headers": kwargs.get("headers") or {}}
        )
        return type(self).response or _Response()

    def post(self, url, **kwargs):
        return self._record("post", url, **kwargs)

    def delete(self, url, **kwargs):
        return self._record("delete", url, **kwargs)

    def get(self, url, **kwargs):
        return self._record("get", url, **kwargs)

    def ws_connect(self, url, **kwargs):
        return self._record("ws", url, **kwargs)


def _bearer(call: dict) -> str:
    header = call["headers"].get("Authorization", "")
    return header[7:] if header.startswith("Bearer ") else ""


def _exercise(body):
    """Run `body(mgr, session_id)` with aiohttp recorded, return the calls."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            try:
                sid = await _seed_session(mgr)
                _RecordingSession.calls = []
                _RecordingSession.response = None
                with patch("aiohttp.ClientSession", _RecordingSession):
                    await body(mgr, sid)
                return list(_RecordingSession.calls)
            finally:
                await mgr.broker_db.close()
                await mgr.shutdown()

    return _run(scenario())


# ---------------------------------------------------------------------------
# R1 — every call the gateway makes to a mind carries a credential
# ---------------------------------------------------------------------------
def test_a_chat_turn_carries_the_mind_s_token() -> None:
    async def body(mgr, sid):
        async for _ in mgr.send_message(sid, "hello"):
            pass

    calls = _exercise(body)
    message = [c for c in calls if c["url"].endswith("/message")]
    assert message, "send_message never reached the mind"
    assert _bearer(message[0]) == "alpha-token"


def test_release_carries_the_mind_s_token() -> None:
    calls = _exercise(lambda mgr, sid: mgr.release_on_mind(sid, "terminal"))
    assert _bearer(calls[0]) == "alpha-token"
    assert calls[0]["url"].endswith("/release")


def test_interrupt_carries_the_mind_s_token() -> None:
    calls = _exercise(lambda mgr, sid: mgr.interrupt_session(sid))
    interrupt = [c for c in calls if c["url"].endswith("/interrupt")]
    assert _bearer(interrupt[0]) == "alpha-token"


def test_kill_carries_the_mind_s_token() -> None:
    calls = _exercise(lambda mgr, sid: mgr._kill_process(sid))
    delete = [c for c in calls if c["method"] == "delete"]
    assert _bearer(delete[0]) == "alpha-token"


def test_rotate_pty_carries_the_mind_s_token() -> None:
    async def body(mgr, sid):
        await mgr._rotate_pty_on_mind(
            session_id=sid, new_claude_sid="conv-2", model="sonnet",
            mind_id=MIND_A, system_prompt="carry forward",
        )

    calls = _exercise(body)
    rotate = [c for c in calls if c["url"].endswith("/rotate-pty")]
    assert _bearer(rotate[0]) == "alpha-token"


def test_spawn_carries_the_mind_s_token() -> None:
    async def body(mgr, sid):
        await mgr._spawn(sid, "sonnet", resume_sid="conv-1", mind_id=MIND_A)

    calls = _exercise(body)
    spawn = [c for c in calls if c["url"].endswith("/sessions")]
    assert _bearer(spawn[0]) == "alpha-token"


# ---------------------------------------------------------------------------
# R2 — each mind's credential is its own
# ---------------------------------------------------------------------------
def test_each_mind_gets_its_own_token() -> None:
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            try:
                alpha = await mgr.mind_auth_headers(MIND_A)
                beta = await mgr.mind_auth_headers(MIND_B)
                return alpha, beta
            finally:
                await mgr.broker_db.close()
                await mgr.shutdown()

    alpha, beta = _run(scenario())
    assert alpha["Authorization"] == "Bearer alpha-token"
    assert beta["Authorization"] == "Bearer beta-token"


def test_a_mind_with_no_stored_token_gets_no_credential() -> None:
    """Not the admin token, and not some other mind's: nothing."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            try:
                await broker.register_mind(
                    mgr.broker_db, mind_id="33333333-3333-4333-8333-333333333333",
                    name="gamma", gateway_url="http://gamma:8420", model="sonnet",
                    harness="claude_cli",
                )
                return await mgr.mind_auth_headers(
                    "33333333-3333-4333-8333-333333333333"
                )
            finally:
                await mgr.broker_db.close()
                await mgr.shutdown()

    with patch.dict(os.environ, {"COMMS_ADMIN_BEARER_TOKEN": "admin-secret"}):
        assert _run(scenario()) == {}


# ---------------------------------------------------------------------------
# R4 — the gateway learns a token only through registration
# ---------------------------------------------------------------------------
def test_registration_stores_the_token() -> None:
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            db = await _broker_db(tmp)
            try:
                return await broker.get_mind_session_token(db, MIND_A)
            finally:
                await db.close()

    assert _run(scenario()) == "alpha-token"


def test_re_registering_replaces_the_token() -> None:
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            db = await _broker_db(tmp)
            try:
                await broker.register_mind(
                    db, mind_id=MIND_A, name="alpha",
                    gateway_url="http://alpha:8420", model="sonnet",
                    harness="claude_cli", session_token="alpha-rotated",  # secret-guard: allow
                )
                return await broker.get_mind_session_token(db, MIND_A)
            finally:
                await db.close()

    assert _run(scenario()) == "alpha-rotated"


def test_re_registering_without_a_token_keeps_the_stored_one() -> None:
    """A mind on an older build, or one that could not read its own file,
    must not lock the gateway out of a mind it can currently reach."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            db = await _broker_db(tmp)
            try:
                await broker.register_mind(
                    db, mind_id=MIND_A, name="alpha",
                    gateway_url="http://alpha:9999", model="opus",
                    harness="claude_cli",
                )
                row = await broker.get_mind_by_id(db, MIND_A)
                token = await broker.get_mind_session_token(db, MIND_A)
                return row["gateway_url"], token
            finally:
                await db.close()

    url, token = _run(scenario())
    assert url == "http://alpha:9999", "the rest of the row still updates"
    assert token == "alpha-token"


# ---------------------------------------------------------------------------
# R5 — the gateway never hands a mind's token to anybody else
# ---------------------------------------------------------------------------
def test_no_mind_read_returns_the_token() -> None:
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            db = await _broker_db(tmp)
            try:
                return (
                    await broker.get_registered_minds(db),
                    await broker.get_mind(db, "alpha"),
                    await broker.get_mind_by_id(db, MIND_A),
                    await broker.update_mind(db, "alpha", model="opus"),
                )
            finally:
                await db.close()

    listing, by_name, by_id, updated = _run(scenario())
    for view in (*listing, by_name, by_id, updated):
        assert "session_token" not in view
        assert "alpha-token" not in str(view)


def test_the_one_accessor_that_does_return_it() -> None:
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            db = await _broker_db(tmp)
            try:
                return (
                    await broker.get_mind_session_token(db, MIND_A),
                    await broker.get_mind_session_token(db, "no-such-mind"),
                )
            finally:
                await db.close()

    found, missing = _run(scenario())
    assert found == "alpha-token"
    assert missing is None


def test_a_database_predating_the_column_is_migrated() -> None:
    """Nothing re-registers until its mind reboots; the column must exist
    before then or every lookup is a 500 on the gateway's own side."""
    async def scenario():
        import aiosqlite
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db")
            legacy = await aiosqlite.connect(path)
            await legacy.executescript(
                """CREATE TABLE minds (
                       mind_id TEXT NOT NULL UNIQUE, name TEXT PRIMARY KEY,
                       gateway_url TEXT NOT NULL, model TEXT NOT NULL,
                       harness TEXT NOT NULL, registered_at REAL NOT NULL,
                       last_seen REAL NOT NULL);
                   INSERT INTO minds VALUES
                       ('old-id', 'old', 'http://old:8420', 'sonnet',
                        'claude_cli', 0, 0);"""
            )
            await legacy.commit()
            await legacy.close()

            db = await broker.init_db(path)
            try:
                return (
                    await broker.get_mind_session_token(db, "old-id"),
                    await broker.get_mind(db, "old"),
                )
            finally:
                await db.close()

    token, row = _run(scenario())
    assert token is None
    assert row["gateway_url"] == "http://old:8420"


# ---------------------------------------------------------------------------
# R11 — a refusal is reported as a refusal
# ---------------------------------------------------------------------------
def test_a_refused_chat_turn_says_the_credential_was_refused() -> None:
    async def body(mgr, sid):
        _RecordingSession.response = _Response(401, '{"error": "unauthorized"}')
        body.events = []
        async for event in mgr.send_message(sid, "hello"):
            body.events.append(event)

    _exercise(body)
    assert body.events, "the refusal produced no event at all"
    last = body.events[-1]
    assert last["is_error"] is True
    assert "credential" in last["result"]


def test_an_unreachable_mind_is_not_reported_as_a_refusal() -> None:
    class _Unreachable(_RecordingSession):
        def _record(self, method, url, **kwargs):
            raise aiohttp.ClientConnectorError(
                aiohttp.client_reqrep.ConnectionKey(
                    "alpha", 8420, False, True, None, None, None
                ),
                OSError("no route"),
            )

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            try:
                sid = await _seed_session(mgr)
                events = []
                with patch("aiohttp.ClientSession", _Unreachable):
                    async for event in mgr.send_message(sid, "hello"):
                        events.append(event)
                return events
            finally:
                await mgr.broker_db.close()
                await mgr.shutdown()

    events = _run(scenario())
    text = " ".join(str(e.get("result", "")) for e in events)
    assert "credential" not in text


# ---------------------------------------------------------------------------
# R11 — a refusal is never folded into a shape that means something else
# ---------------------------------------------------------------------------
def _refusing(status: int = 401):
    class _Refusing(_RecordingSession):
        def _record(self, method, url, **kwargs):
            type(self).calls.append({"method": method, "url": url,
                                     "headers": kwargs.get("headers") or {}})
            return _Response(status, '{"error": "unauthorized"}')

    return _Refusing


def _with_refusing_mind(body, status: int = 401):
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            try:
                sid = await _seed_session(mgr)
                _RecordingSession.calls = []
                with patch("aiohttp.ClientSession", _refusing(status)):
                    return await body(mgr, sid)
            finally:
                await mgr.broker_db.close()
                await mgr.shutdown()

    return _run(scenario())


def test_a_refused_release_is_not_reported_as_nothing_to_release() -> None:
    """False means the harness is already gone. A refusal means it is still
    running, and a caller reading the two alike respawns beside it."""
    async def body(mgr, sid):
        try:
            await mgr.release_on_mind(sid, "terminal")
        except MindRefusedCredential as exc:
            return str(exc)
        return None

    message = _with_refusing_mind(body)
    assert message is not None, "the refusal was swallowed and returned as False"
    assert "credential" in message


def test_an_adoption_stops_rather_than_running_two_harnesses() -> None:
    """One live harness process per conversation. Retargeting ownership over a
    terminal that is still running is two processes on one transcript."""
    async def body(mgr, sid):
        with pytest.raises(MindRefusedCredential):
            await mgr.activate_session(
                sid, "telegram", "555",
                owner_type="telegram", owner_ref="555",
            )
        row = await mgr._get_row(sid)
        return row["owner_type"], row["owner_ref"]

    owner_type, owner_ref = _with_refusing_mind(body)
    assert (owner_type, owner_ref) == ("telegram", "123"), (
        "ownership was retargeted over a terminal that was never released"
    )


def test_a_refused_interrupt_is_not_returned_as_the_interrupt_s_result() -> None:
    """`{"error": "unauthorized"}` is a dict, so it used to pass the result
    guard and report a still-running turn as interrupted."""
    async def body(mgr, sid):
        mgr._procs[sid] = {"_mind_url": "http://alpha:8420"}
        with pytest.raises(MindRefusedCredential):
            await mgr.interrupt_session(sid)
        return True

    assert _with_refusing_mind(body) is True


def test_a_refused_kill_says_the_harness_is_still_running(caplog) -> None:
    """The row closes either way, but claiming the kill landed is how a tmux
    session and its context leak once per ended conversation."""
    async def body(mgr, sid):
        await mgr._kill_process(sid)
        return True

    with caplog.at_level("ERROR"):
        assert _with_refusing_mind(body) is True
    assert any("still" in r.message or "refused" in r.message for r in caplog.records)
    assert not any("Killed session" in r.getMessage() for r in caplog.records)


def test_a_refused_rotation_does_not_blame_the_pane() -> None:
    async def body(mgr, sid):
        with pytest.raises(MindRefusedCredential):
            await mgr._rotate_pty_on_mind(
                session_id=sid, new_claude_sid="conv-2", model="sonnet",
                mind_id=MIND_A, system_prompt="carry forward",
            )
        return True

    assert _with_refusing_mind(body) is True


def test_a_503_from_a_mind_that_cannot_read_its_token_is_also_a_refusal() -> None:
    """A mind that cannot establish its own credential answers 503, and that
    is the same kind of no — the gateway must not read it as a dead mind."""
    async def body(mgr, sid):
        try:
            await mgr.release_on_mind(sid, "terminal")
        except MindRefusedCredential:
            return True
        return False

    assert _with_refusing_mind(body, status=503) is True


# ---------------------------------------------------------------------------
# R11's other half — "or cannot reach it" is its own answer too
# ---------------------------------------------------------------------------
def _unreachable_session():
    class _Unreachable(_RecordingSession):
        def _record(self, method, url, **kwargs):
            raise aiohttp.ClientConnectorError(
                aiohttp.client_reqrep.ConnectionKey(
                    "alpha", 8420, False, True, None, None, None
                ),
                OSError("no route"),
            )

    return _Unreachable


def _with_unreachable_mind(body):
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            try:
                sid = await _seed_session(mgr)
                with patch("aiohttp.ClientSession", _unreachable_session()):
                    return await body(mgr, sid)
            finally:
                await mgr.broker_db.close()
                await mgr.shutdown()

    return _run(scenario())


def test_an_unreachable_release_is_not_nothing_to_release() -> None:
    """False means the harness is already gone. Unreachable means nobody
    knows — and the adoption path acts on False by proceeding."""
    async def body(mgr, sid):
        with pytest.raises(MindCallFailed):
            await mgr.release_on_mind(sid, "terminal")
        return True

    assert _with_unreachable_mind(body) is True


def test_an_unreachable_mind_stops_an_adoption_too() -> None:
    async def body(mgr, sid):
        with pytest.raises(MindCallFailed):
            await mgr.activate_session(
                sid, "telegram", "555",
                owner_type="telegram", owner_ref="555",
            )
        row = await mgr._get_row(sid)
        return row["owner_type"], row["owner_ref"]

    assert _with_unreachable_mind(body) == ("telegram", "123")


def test_suspending_still_reaches_suspended_when_a_release_fails() -> None:
    """Suspending is not adopting — nothing is about to start a second harness
    — so a release that could not be delivered must not leave the row in
    neither state."""
    async def body(mgr, sid):
        await mgr.suspend_session(sid)
        row = await mgr._get_row(sid)
        return row["status"]

    assert _with_unreachable_mind(body) == "suspended"


def test_an_unreachable_kill_is_not_logged_as_a_kill(caplog) -> None:
    async def body(mgr, sid):
        await mgr._kill_process(sid)
        return True

    with caplog.at_level("INFO"):
        assert _with_unreachable_mind(body) is True
    assert not any("Killed session" in r.getMessage() for r in caplog.records)


def test_an_unreachable_rotation_does_not_report_an_absent_terminal() -> None:
    async def body(mgr, sid):
        with pytest.raises(MindCallFailed):
            await mgr._rotate_pty_on_mind(
                session_id=sid, new_claude_sid="conv-2", model="sonnet",
                mind_id=MIND_A, system_prompt="carry forward",
            )
        return True

    assert _with_unreachable_mind(body) is True


def test_a_refused_models_listing_is_not_an_empty_catalog() -> None:
    """An empty list reaches the console as "this mind offers nothing" and
    makes a model change fail with the wrong reason entirely."""
    async def body(mgr, sid):
        with pytest.raises(MindRefusedCredential):
            await mgr.mind_models(MIND_A)
        return True

    assert _with_refusing_mind(body) is True


def test_a_broker_read_failure_is_this_gateway_s_fault_not_the_mind_s() -> None:
    """An uncredentialed call gets a 401 whose handler tells the operator to
    re-register the mind — a remedy aimed at the mind for a fault in here."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            try:
                await mgr.broker_db.close()
                with pytest.raises(MindCallFailed):
                    await mgr.mind_auth_headers(MIND_A)
                return True
            finally:
                await mgr.shutdown()

    assert _run(scenario()) is True
