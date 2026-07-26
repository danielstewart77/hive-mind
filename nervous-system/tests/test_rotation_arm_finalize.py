"""Finalize-on-user-turn rotation — NS side.

The rotation Stop hook fires on an assistant turn. Clearing the session
there can kill the old session mid-reply to a message that landed during the
rotation window (the Hex-stall failure mode). Instead the hook ARMS the
session (``arm_rotation``); for chat surfaces the actual swap is deferred to
the next user turn, performed inside ``send_message`` via
``_finalize_rotation``. The browser terminal never calls ``send_message``
(its keystrokes are a raw pty byte bridge), so for terminal-owned sessions
``arm_rotation`` finalizes immediately instead.

Covers:
- arm_rotation sets the flag on a chat-surface active session (and no-ops
  with none).
- arm_rotation finalizes immediately for a terminal-owned session, returning
  ``rotated_to``.
- _finalize_rotation retires the armed session and creates a replacement,
  rebinding the surface; disarms safely when client_ref is missing rather
  than stranding the conversation.
- kill_session publishes ``rotated_to`` on the session_closed event when the
  kill is the retiring half of a rotation swap.
- send_message redirects an armed session's turn to the fresh session, and
  leaves un-armed delivery untouched.
- record_turn appends to the session_turns ledger for the active session,
  and no-ops when there is none.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

from comms.models import ModelRegistry, Provider
from comms.sessions import SessionManager


def _run(coro):
    return asyncio.run(coro)


def _registry() -> ModelRegistry:
    return ModelRegistry({"anthropic": Provider(name="anthropic")}, {"opus": "anthropic"})


async def _make_manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager(_registry())
    await mgr.start()
    return mgr


async def _seed_session(
    mgr: SessionManager,
    *,
    owner_type: str,
    client_ref: str,
    owner_ref: str | None = None,
    bind_active: bool = True,
) -> str:
    """Insert a running session; optionally bind it active. Returns session_id.

    ``owner_ref`` defaults to ``client_ref`` (true for chat surfaces, where
    the two coincide); pass it explicitly for terminal-owned sessions, where
    ``owner_ref`` is the fixed value ``"terminal"`` and ``client_ref`` is a
    per-tab id.
    """
    session_id = "sess-" + client_ref
    now = time.time()
    await mgr._db.execute(
        """INSERT INTO sessions (id, owner_type, owner_ref, model, created_at, last_active, status, mind_id)
           VALUES (?, ?, ?, 'opus', ?, ?, 'running', 'ada')""",
        (session_id, owner_type, owner_ref if owner_ref is not None else client_ref, now, now),
    )
    if bind_active:
        await mgr._db.execute(
            """INSERT INTO active_sessions (client_type, client_ref, session_id)
               VALUES (?, ?, ?)""",
            (owner_type, client_ref, session_id),
        )
    await mgr._db.commit()
    return session_id


async def _armed_flag(mgr: SessionManager, session_id: str) -> int:
    cur = await mgr._db.execute(
        "SELECT rotation_armed FROM sessions WHERE id = ?", (session_id,)
    )
    row = await cur.fetchone()
    return row["rotation_armed"] if row else -1


async def _active_binding(mgr: SessionManager, client_type: str, client_ref: str) -> str | None:
    cur = await mgr._db.execute(
        "SELECT session_id FROM active_sessions WHERE client_type = ? AND client_ref = ?",
        (client_type, client_ref),
    )
    row = await cur.fetchone()
    return row["session_id"] if row else None


# ---------------------------------------------------------------------------
# arm_rotation
# ---------------------------------------------------------------------------

def test_arm_rotation_sets_flag_on_active_session() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                sid = await _seed_session(mgr, owner_type="telegram", client_ref="123")
                assert await _armed_flag(mgr, sid) == 0
                result = await mgr.arm_rotation("telegram", "123")
                assert result == {"ok": True, "session_id": sid}
                assert await _armed_flag(mgr, sid) == 1
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_arm_rotation_no_active_session() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                result = await mgr.arm_rotation("telegram", "nobody")
                assert result["ok"] is False
            finally:
                await mgr.shutdown()

    _run(scenario())


# ---------------------------------------------------------------------------
# _finalize_rotation
# ---------------------------------------------------------------------------

def test_finalize_swaps_and_rebinds_surface() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                old = await _seed_session(mgr, owner_type="telegram", client_ref="123")
                await mgr.arm_rotation("telegram", "123")

                created: dict = {}

                async def fake_create_session(*, owner_type, owner_ref, client_ref, mind_id, **kw):
                    created.update(
                        owner_type=owner_type, owner_ref=owner_ref,
                        client_ref=client_ref, mind_id=mind_id,
                    )
                    new_id = "sess-new"
                    now = time.time()
                    await mgr._db.execute(
                        """INSERT INTO sessions (id, owner_type, owner_ref, model, created_at, last_active, status, mind_id)
                           VALUES (?, ?, ?, 'opus', ?, ?, 'running', ?)""",
                        (new_id, owner_type, owner_ref, now, now, mind_id),
                    )
                    await mgr._db.execute(
                        """INSERT OR REPLACE INTO active_sessions (client_type, client_ref, session_id)
                           VALUES (?, ?, ?)""",
                        (owner_type, client_ref, new_id),
                    )
                    await mgr._db.commit()
                    return {"id": new_id}

                mgr.create_session = fake_create_session  # type: ignore[assignment]

                session = await mgr._get_row(old)
                new_id = await mgr._finalize_rotation(session)

                assert new_id == "sess-new"
                # Replacement created for the same surface identity.
                assert created == {
                    "owner_type": "telegram", "owner_ref": "123",
                    "client_ref": "123", "mind_id": "ada",
                }
                # Old session retired, surface rebound to the new session.
                cur = await mgr._db.execute("SELECT status FROM sessions WHERE id = ?", (old,))
                assert (await cur.fetchone())["status"] == "closed"
                assert await _active_binding(mgr, "telegram", "123") == "sess-new"
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_finalize_without_client_ref_disarms_and_stays() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                # No active binding → no client_ref to rebind a replacement.
                sid = await _seed_session(
                    mgr, owner_type="telegram", client_ref="123", bind_active=False
                )
                await mgr._db.execute(
                    "UPDATE sessions SET rotation_armed = 1 WHERE id = ?", (sid,)
                )
                await mgr._db.commit()

                async def fail_create(*a, **k):  # must not be called
                    raise AssertionError("create_session called despite missing client_ref")

                mgr.create_session = fail_create  # type: ignore[assignment]

                session = await mgr._get_row(sid)
                new_id = await mgr._finalize_rotation(session)

                assert new_id is None
                # Disarmed and left alive so the caller delivers normally.
                assert await _armed_flag(mgr, sid) == 0
                cur = await mgr._db.execute("SELECT status FROM sessions WHERE id = ?", (sid,))
                assert (await cur.fetchone())["status"] == "running"
            finally:
                await mgr.shutdown()

    _run(scenario())


# ---------------------------------------------------------------------------
# send_message redirect
# ---------------------------------------------------------------------------

def test_send_message_armed_redirects_to_new_session() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                old = await _seed_session(mgr, owner_type="telegram", client_ref="123")
                await mgr.arm_rotation("telegram", "123")

                async def fake_finalize(session):
                    return "sess-new"

                forwarded: dict = {}

                async def fake_forward(session_id, content, images):
                    forwarded.update(session_id=session_id, content=content)
                    yield {"type": "text", "text": "from new session"}

                mgr._finalize_rotation = fake_finalize  # type: ignore[assignment]
                mgr._forward_to_session = fake_forward  # type: ignore[assignment]

                events = [ev async for ev in mgr.send_message(old, "hello")]

                assert forwarded == {"session_id": "sess-new", "content": "hello"}
                assert events == [{"type": "text", "text": "from new session"}]
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_send_message_not_armed_never_finalizes() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                sid = await _seed_session(mgr, owner_type="telegram", client_ref="123")

                async def fail_finalize(session):  # must not be called
                    raise AssertionError("finalize called for un-armed session")

                async def fail_forward(session_id, content, images):
                    raise AssertionError("forward called for un-armed session")
                    yield  # pragma: no cover

                async def stop_spawn(*a, **k):
                    raise RuntimeError("stop before real spawn")

                mgr._finalize_rotation = fail_finalize  # type: ignore[assignment]
                mgr._forward_to_session = fail_forward  # type: ignore[assignment]
                mgr._spawn = stop_spawn  # type: ignore[assignment]

                # Un-armed session skips the swap and takes the normal delivery
                # path, which we cut short at _spawn. The point is that neither
                # the finalize nor the forward seam fires.
                try:
                    async for _ in mgr.send_message(sid, "hello"):
                        pass
                except RuntimeError as exc:
                    assert "stop before real spawn" in str(exc)
            finally:
                await mgr.shutdown()

    _run(scenario())


# ---------------------------------------------------------------------------
# arm_rotation — terminal-owned sessions finalize immediately
# ---------------------------------------------------------------------------

def test_arm_rotation_terminal_finalizes_immediately() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                old = await _seed_session(
                    mgr, owner_type="web", owner_ref="terminal", client_ref="terminal-abc"
                )

                async def fake_create_session(*, owner_type, owner_ref, client_ref, mind_id, **kw):
                    new_id = "sess-new"
                    now = time.time()
                    await mgr._db.execute(
                        """INSERT INTO sessions (id, owner_type, owner_ref, model, created_at, last_active, status, mind_id)
                           VALUES (?, ?, ?, 'opus', ?, ?, 'running', ?)""",
                        (new_id, owner_type, owner_ref, now, now, mind_id),
                    )
                    await mgr._db.execute(
                        """INSERT OR REPLACE INTO active_sessions (client_type, client_ref, session_id)
                           VALUES (?, ?, ?)""",
                        (owner_type, client_ref, new_id),
                    )
                    await mgr._db.commit()
                    return {"id": new_id}

                mgr.create_session = fake_create_session  # type: ignore[assignment]

                result = await mgr.arm_rotation("web", "terminal-abc")

                # Finalizes right away — no arming, no waiting on send_message.
                assert result == {"ok": True, "session_id": old, "rotated_to": "sess-new"}
                cur = await mgr._db.execute("SELECT status FROM sessions WHERE id = ?", (old,))
                assert (await cur.fetchone())["status"] == "closed"
                assert await _active_binding(mgr, "web", "terminal-abc") == "sess-new"
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_arm_rotation_chat_surface_unaffected_by_terminal_branch() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                sid = await _seed_session(mgr, owner_type="telegram", client_ref="123")

                async def fail_create(*a, **k):  # must not be called
                    raise AssertionError("create_session called for a chat-surface arm")

                mgr.create_session = fail_create  # type: ignore[assignment]

                result = await mgr.arm_rotation("telegram", "123")

                assert result == {"ok": True, "session_id": sid}
                assert await _armed_flag(mgr, sid) == 1
                cur = await mgr._db.execute("SELECT status FROM sessions WHERE id = ?", (sid,))
                assert (await cur.fetchone())["status"] == "running"
            finally:
                await mgr.shutdown()

    _run(scenario())


# ---------------------------------------------------------------------------
# kill_session — rotated_to on the published event
# ---------------------------------------------------------------------------

def test_kill_session_publishes_rotated_to() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                sid = await _seed_session(mgr, owner_type="telegram", client_ref="123")
                queue: asyncio.Queue = asyncio.Queue(maxsize=10)
                mgr._observer_queues[sid] = {queue}

                await mgr.kill_session(sid, rotated_to="sess-new")

                event = queue.get_nowait()
                assert event == {
                    "type": "session_closed", "session_id": sid, "rotated_to": "sess-new"
                }
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_kill_session_without_rotated_to_omits_key() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                sid = await _seed_session(mgr, owner_type="telegram", client_ref="456")
                queue: asyncio.Queue = asyncio.Queue(maxsize=10)
                mgr._observer_queues[sid] = {queue}

                await mgr.kill_session(sid)

                event = queue.get_nowait()
                assert event == {"type": "session_closed", "session_id": sid}
                assert "rotated_to" not in event
            finally:
                await mgr.shutdown()

    _run(scenario())


# ---------------------------------------------------------------------------
# record_turn
# ---------------------------------------------------------------------------

def test_record_turn_appends_to_ledger() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                sid = await _seed_session(
                    mgr, owner_type="web", owner_ref="terminal", client_ref="terminal-abc"
                )

                result = await mgr.record_turn("web", "terminal-abc", "user", "hello from tile")

                assert result == {"ok": True, "session_id": sid}
                late = await mgr.get_late_turns("web", "terminal-abc", since=0)
                assert late["session_id"] == sid
                assert [t["role"] for t in late["turns"]] == ["user"]
                assert late["turns"][0]["content"] == "hello from tile"
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_record_turn_no_active_session() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                result = await mgr.record_turn("web", "nobody-home", "user", "hi")
                assert result == {"ok": False, "error": "no active session"}
            finally:
                await mgr.shutdown()

    _run(scenario())
