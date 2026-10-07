"""Opening a conversation in a browser tile hands it to the terminal.

A tile reaches `attach-pty` directly and nothing else binds it, so the
conversation sat in `sessions` with no row in `active_sessions`:
`_routing_for` yielded no `client_ref` for the pane's environment and the
Stop hook's liveness pre-flight was answered no on every fire, so a rotation
never armed. One pane ran eight days to 1834 messages and `Prompt is too
long`.

The repair is an adoption. A second binding beside the chat's would point the
chat at a pane-hosted conversation, and `send_message` decides whether a
harness is running from its own process table, which knows nothing of tmux —
so the chat would spawn a rival `--resume` and two processes would append to
one transcript.

Covers:
- the terminal takes ownership under a key derived from the conversation.
- the chat's own key is left pointing where it was.
- the chat-side harness is released before ownership moves.
- a refused release aborts the handover and changes nothing.
- a chat-armed rotation is cleared, since only the chat could finalize it.
- reattaching the same conversation releases nothing.
- two conversations owned by one chat never share a surface key.
- a prefix-resolved id binds the conversation's real id.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

from comms import sessions as sessions_mod
from comms.sessions import SessionManager

TELEGRAM = "telegram:skippy-uuid"
CHAT = "8776938611"


def _run(coro):
    return asyncio.run(coro)


async def _make_manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager()
    await mgr.start()
    if mgr._dashboard_sweep_task:
        mgr._dashboard_sweep_task.cancel()
        mgr._dashboard_sweep_task = None
    return mgr


async def _seed(
    mgr: SessionManager,
    session_id: str,
    *,
    owner_type: str = TELEGRAM,
    owner_ref: str = CHAT,
    status: str = "running",
    claude_sid: str = "conv-1",
    rotation_armed: int = 0,
) -> None:
    now = time.time()
    await mgr._db.execute(
        """INSERT INTO sessions
           (id, owner_type, owner_ref, model, claude_sid, summary, created_at,
            last_active, status, mind_id, rotation_armed)
           VALUES (?, ?, ?, 'opus', ?, 'seeded', ?, ?, ?, 'skippy-uuid', ?)""",
        (session_id, owner_type, owner_ref, claude_sid, now - 60, now, status, rotation_armed),
    )
    await mgr._db.commit()


async def _row(mgr: SessionManager, session_id: str) -> dict:
    rows = await mgr._db.execute_fetchall(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    )
    return dict(rows[0])


async def _bindings(mgr: SessionManager, session_id: str) -> list[tuple[str, str]]:
    rows = await mgr._db.execute_fetchall(
        "SELECT client_type, client_ref FROM active_sessions WHERE session_id = ?",
        (session_id,),
    )
    return sorted((r["client_type"], r["client_ref"]) for r in rows)


def _releasing(mgr: SessionManager) -> list[tuple[str, str]]:
    """Record releases, and the owner at the moment each one happened."""
    seen: list[tuple[str, str]] = []

    async def _release(session_id, surface):
        row = await _row(mgr, session_id)
        seen.append((surface, row["owner_type"]))
        return True

    mgr.release_on_mind = _release  # type: ignore[assignment]
    return seen


def test_the_terminal_takes_the_conversation_under_its_own_key():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "in-the-pane")
                _releasing(mgr)

                routing = await mgr.adopt_into_terminal("in-the-pane")

                assert routing == {
                    "owner_type": "terminal",
                    "owner_ref": "terminal-in-the-pane",
                    "client_ref": "terminal-in-the-pane",
                }
                row = await _row(mgr, "in-the-pane")
                assert row["owner_type"] == "terminal"
                assert row["owner_ref"] == "terminal-in-the-pane"
                assert row["claude_sid"] == "conv-1"  # the thread is untouched
                assert await _bindings(mgr, "in-the-pane") == [
                    ("terminal", "terminal-in-the-pane")
                ]
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_the_chats_own_key_keeps_pointing_where_it_was():
    """The chat must not be retargeted at a pane-hosted conversation.

    `send_message` reads its own process table to decide whether a harness is
    running and never sees tmux, so a chat pointed here would spawn a rival
    `--resume` on the same transcript.
    """
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "chatting")
                await _seed(mgr, "in-the-pane", claude_sid="conv-2")
                await mgr._db.execute(
                    """INSERT INTO active_sessions (client_type, client_ref, session_id)
                       VALUES (?, ?, 'chatting')""",
                    (TELEGRAM, CHAT),
                )
                await mgr._db.commit()
                _releasing(mgr)

                await mgr.adopt_into_terminal("in-the-pane")

                assert await _bindings(mgr, "chatting") == [(TELEGRAM, CHAT)]
                active = await mgr.get_active_session(TELEGRAM, CHAT)
                assert active is not None and active["id"] == "chatting"
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_the_chat_side_harness_is_released_before_ownership_moves():
    """Retargeting first would leave a stream-json process on the transcript
    with the row already claiming the pane owns it."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "in-the-pane")
                released = _releasing(mgr)

                await mgr.adopt_into_terminal("in-the-pane")

                # The chat-side process, not the pane this attach is for.
                assert released == [("stream", TELEGRAM)]
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_a_refused_release_aborts_the_handover():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "in-the-pane")

                async def _release(session_id, surface):
                    raise sessions_mod.MindRefusedCredential("mind said no")

                mgr.release_on_mind = _release  # type: ignore[assignment]

                raised = False
                try:
                    await mgr.adopt_into_terminal("in-the-pane")
                except sessions_mod.MindCallFailed:
                    raised = True

                assert raised
                row = await _row(mgr, "in-the-pane")
                assert row["owner_type"] == TELEGRAM
                assert row["owner_ref"] == CHAT
                assert await _bindings(mgr, "in-the-pane") == []
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_a_chat_armed_rotation_is_cleared_on_the_way_in():
    """Only `send_message` finalizes a chat-armed rotation and the chat will
    never call it for this conversation again, so the flag would strand the
    session on a rotation nothing can complete."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "armed-one", rotation_armed=sessions_mod.ROTATION_ARMED_CHAT)
                _releasing(mgr)

                await mgr.adopt_into_terminal("armed-one")

                assert (await _row(mgr, "armed-one"))["rotation_armed"] == 0
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_a_staged_terminal_rotation_survives_a_reattach():
    """A staged rotation is the pane's own and only the pane can fire it."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(
                    mgr, "staged",
                    owner_type="terminal", owner_ref="terminal-staged",
                    rotation_armed=sessions_mod.ROTATION_ARMED_TERMINAL,
                )
                released = _releasing(mgr)

                await mgr.adopt_into_terminal("staged")

                assert released == []  # a reattach hands over nothing
                row = await _row(mgr, "staged")
                assert row["rotation_armed"] == sessions_mod.ROTATION_ARMED_TERMINAL
                assert await _bindings(mgr, "staged") == [("terminal", "terminal-staged")]
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_two_conversations_owned_by_one_chat_never_share_a_key():
    """`active_sessions` is keyed `(client_type, client_ref)`, so a shared key
    means one attach silently unbinds the other — and then the Stop hook's
    pre-flight answers yes on somebody else's binding and the rotation stages
    onto the wrong pane."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "pane-a")
                await _seed(mgr, "pane-b", claude_sid="conv-2")
                _releasing(mgr)

                await mgr.adopt_into_terminal("pane-a")
                await mgr.adopt_into_terminal("pane-b")

                assert await _bindings(mgr, "pane-a") == [("terminal", "terminal-pane-a")]
                assert await _bindings(mgr, "pane-b") == [("terminal", "terminal-pane-b")]
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_a_prefix_id_binds_the_conversations_real_id():
    """`get_session` resolves an id by prefix. Writing the prefix into
    `active_sessions` fails the foreign key, unhandled and before the
    handshake — which the tile reads as a mind with no attach route at all."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "abcdef12-3456-7890-abcd-ef1234567890")
                _releasing(mgr)

                routing = await mgr.adopt_into_terminal("abcdef12")

                assert routing["client_ref"] == (
                    "terminal-abcdef12-3456-7890-abcd-ef1234567890"
                )
                assert await _bindings(
                    mgr, "abcdef12-3456-7890-abcd-ef1234567890"
                ) == [("terminal", "terminal-abcdef12-3456-7890-abcd-ef1234567890")]
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_an_old_binding_under_another_key_is_replaced_not_joined():
    """The console's resume path binds under its own `web`/`terminal-<uuid>`
    key. Left in place beside the new one, `_routing_for` reads one row with
    LIMIT 1 and which `client_ref` the pane receives becomes a matter of row
    order — and the pre-flight pair the hook sends can then match neither."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "resumed")
                await mgr._db.execute(
                    """INSERT INTO active_sessions (client_type, client_ref, session_id)
                       VALUES ('web', 'terminal-5f3c', 'resumed')"""
                )
                await mgr._db.commit()
                _releasing(mgr)

                await mgr.adopt_into_terminal("resumed")

                assert await _bindings(mgr, "resumed") == [
                    ("terminal", "terminal-resumed")
                ]
                routing = await mgr._routing_for(await _row(mgr, "resumed"))
                assert routing["client_ref"] == "terminal-resumed"
            finally:
                await mgr.shutdown()

    _run(scenario())
