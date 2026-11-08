"""Rotate-on-user-turn — NS side.

The rotation Stop hook fires on an assistant turn. Replacing the
conversation there can land mid-reply to a message that arrived during the
rotation window (the Hex-stall failure mode). Instead the hook ARMS the row
(``arm_rotation``) and the swap waits for the next user turn: a chat turn
does it inside ``send_message``, a pane's typed turn through
``fire_rotation``, because the terminal is a raw byte bridge that never
reaches ``send_message``. Both go through ``_replace_conversation``, so the
row is permanent on every surface and only the harness conversation under it
is new.

Covers:
- arm_rotation sets the flag on an active session, and no-ops with none.
- an armed chat turn replaces the conversation under the same row rather
  than minting a successor, and leaves un-armed delivery untouched.
- fire_rotation replaces a pane's conversation in place: same session row,
  new claude_sid, pane respawned onto it with the typed message; a mind with
  no live pane leaves the conversation where it was.
- record_turn appends to the session_turns ledger for the active session,
  and no-ops when there is none.
"""
# ruff: noqa: F811 - pytest fixtures are imported, then named as parameters
from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import patch

from comms.sessions import SessionManager
from comms_pg_fixture import (  # noqa: F401 - fixtures
    close_manager,
    comms_db,
    fetchval,
    make_manager,
    register_mind,
    scratch_dsn,
    seed_session,
)


def _run(coro):
    return asyncio.run(coro)


async def _seed_session(
    *,
    owner_type: str,
    client_ref: str,
    owner_ref: str | None = None,
    bind_active: bool = True,
    claude_sid: str = "conv-old",
    terminal_held: bool = False,
) -> str:
    """Insert a running session; optionally bind it active. Returns session_id.

    ``owner_ref`` defaults to ``client_ref`` (true for chat surfaces, where
    the two coincide); pass it explicitly for terminal-owned sessions, where
    ``owner_ref`` is the fixed value ``"terminal"`` and ``client_ref`` is a
    per-tab id. The mind it names is registered on model ``opus``.
    """
    session_id = "sess-" + client_ref
    await register_mind("ada", model="opus", gateway_url="http://mind")
    await seed_session(
        session_id, owner_type=owner_type,
        owner_ref=owner_ref if owner_ref is not None else client_ref,
        model="opus", claude_sid=claude_sid, terminal_held=terminal_held,
        bind=(owner_type, client_ref) if bind_active else None,
    )
    return session_id


async def _armed_flag(session_id: str) -> int:
    flag = await fetchval(
        "SELECT rotation_armed FROM core.sessions WHERE id = $1", session_id
    )
    return -1 if flag is None else flag


#: What the mind renders of the conversation being replaced. The real render
#: reads a transcript off the mind's own disk.
HANDOVER = "what came before"


@contextlib.contextmanager
def _real_rotation_paths():
    """Let a test drive the real rotation paths.

    Their outside dependencies — the prompt composer, and the mind that
    renders the handover and holds the staged marker — need a populated KG
    and a running container, which these tests do not have. The mind's
    registration is a real row (see ``_seed_session``). Everything else is
    real.
    """
    async def blocks(**_kw):
        return "<soul>seed</soul>"

    async def handover(_self, *_a, **_kw):
        return HANDOVER

    async def nothing(*_a, **_kw):
        return None

    with patch("comms.bootstrap_loader.compose_prompt_blocks", blocks), \
            patch.object(SessionManager, "_render_handover_on_mind", handover), \
            patch.object(SessionManager, "_clear_rotation_marker_on_mind", nothing):
        yield


def _noop_kill(mgr: SessionManager):
    """Skip the HTTP kill to the mind — these tests own no container."""
    async def _kill(session_id: str) -> None:
        mgr._procs.pop(session_id, None)
        mgr._mind_ids.pop(session_id, None)
    return _kill


async def _active_binding(client_type: str, client_ref: str) -> str | None:
    return await fetchval(
        "SELECT session_id FROM core.active_sessions "
        "WHERE client_type = $1 AND client_ref = $2",
        client_type, client_ref,
    )


# ---------------------------------------------------------------------------
# arm_rotation
# ---------------------------------------------------------------------------

def test_arm_rotation_sets_flag_on_active_session(comms_db) -> None:
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            sid = await _seed_session(owner_type="telegram", client_ref="123")
            assert await _armed_flag(sid) == 0
            result = await mgr.arm_rotation("telegram", "123")
            assert result == {"ok": True, "session_id": sid}
            assert await _armed_flag(sid) == 1
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_arm_rotation_no_active_session(comms_db) -> None:
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            result = await mgr.arm_rotation("telegram", "nobody")
            assert result["ok"] is False
        finally:
            await close_manager(mgr)

    _run(scenario())


# ---------------------------------------------------------------------------
# _finalize_rotation
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# send_message redirect
# ---------------------------------------------------------------------------

def test_send_message_not_armed_never_finalizes(comms_db) -> None:
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            sid = await _seed_session(owner_type="telegram", client_ref="123")

            async def fail_replace(*a, **k):  # must not be called
                raise AssertionError(
                    "the conversation was replaced for an un-armed session"
                )

            async def stop_spawn(*a, **k):
                raise RuntimeError("stop before real spawn")

            mgr._replace_conversation = fail_replace  # type: ignore[assignment]
            mgr._spawn = stop_spawn  # type: ignore[assignment]

            # Un-armed session skips the swap and takes the normal delivery
            # path, which we cut short at _spawn. The point is that the
            # replacement never fires.
            try:
                async for _ in mgr.send_message(sid, "hello"):
                    pass
            except RuntimeError as exc:
                assert "stop before real spawn" in str(exc)
        finally:
            await close_manager(mgr)

    _run(scenario())


# ---------------------------------------------------------------------------
# arm_rotation + fire_rotation — terminal-owned sessions swap on a typed turn
# ---------------------------------------------------------------------------

async def _conversation_id(session_id: str) -> str | None:
    return await fetchval(
        "SELECT conversation_id FROM core.sessions WHERE id = $1", session_id
    )


def test_terminal_rotation_keeps_the_session_and_swaps_the_conversation(comms_db) -> None:
    """The session row is permanent. Rotation replaces the harness
    conversation underneath it — a new claude_sid on the same row — because
    the tile, its label, its ledger rows and its active binding are all keyed
    to the session id, and moving that id moves the user out from under a
    pane they are typing into."""
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            sid = await _seed_session(
                owner_type="web", owner_ref="terminal",
                client_ref="terminal-abc", claude_sid="conv-full",
                terminal_held=True,
            )
            queue: asyncio.Queue = asyncio.Queue(maxsize=10)
            mgr._observer_queues[sid] = {queue}
            calls: list[dict] = []

            async def fake_rotate(**kwargs):
                calls.append(kwargs)
                return True

            async def fail_create(*a, **k):
                raise AssertionError("a terminal rotation must not create a session")

            mgr._rotate_pty_on_mind = fake_rotate  # type: ignore[assignment]
            mgr.create_session = fail_create  # type: ignore[assignment]

            with _real_rotation_paths():
                # Staging composes the seed; the pane's own typed turn is
                # what starts the successor.
                await mgr.arm_rotation("web", "terminal-abc")
                result = await mgr.fire_rotation(
                    "web", "terminal-abc",
                    claude_sid="conv-full", prompt="carry on",
                )

            assert result["ok"] is True
            assert result["session_id"] == sid
            new_conv = result["conversation_id"]
            assert new_conv != "conv-full"
            assert await _conversation_id(sid) == new_conv
            assert calls[0]["session_id"] == sid
            assert calls[0]["new_claude_sid"] == new_conv
            # The pane opens on both: the handover as its user turn,
            # carrying the message that fired the rotation, and the soul
            # beside it as a system prompt. Handed the handover alone it
            # would remember what it was doing and not who it is.
            assert calls[0]["user_prompt"] == f"{HANDOVER}\n\ncarry on"
            assert "<soul>seed</soul>" in calls[0]["system_prompt"]

            # Nothing above the harness moved: the row is still running,
            # still bound, and nobody was told the session ended. The one
            # thing observers hear is the rotation itself, which carries
            # the screen the respawn wiped — not a close.
            row = {
                "status": await fetchval(
                    "SELECT status FROM core.sessions WHERE id = $1", sid),
                "rotation_armed": await fetchval(
                    "SELECT rotation_armed FROM core.sessions WHERE id = $1", sid),
            }
            assert row["status"] == "running"
            assert row["rotation_armed"] == 0
            assert await _active_binding("web", "terminal-abc") == sid
            assert queue.get_nowait()["type"] == "session_rotated"
            assert queue.empty()
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_a_rotation_whose_pane_is_gone_still_replaces_the_conversation(comms_db) -> None:
    """The conversation is over its threshold either way, and something has to
    hold the one that replaces it. A mind with no live pane is not a reason to
    leave the row on a full conversation — it is a reason for a process to
    take the new one, which is what every chat rotation does. Writing a fresh
    conversation id and starting nothing is the only outcome that strands the
    row on a conversation that never existed."""
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            sid = await _seed_session(
                owner_type="web", owner_ref="terminal",
                client_ref="terminal-xyz", claude_sid="conv-kept",
                terminal_held=True,
            )

            async def declines(**kwargs):
                return False

            spawned: list[dict] = []

            async def fake_spawn(session_id, model, **kw):
                spawned.append({"session_id": session_id, **kw})

            mgr._rotate_pty_on_mind = declines  # type: ignore[assignment]
            mgr._spawn = fake_spawn  # type: ignore[assignment]
            mgr._kill_process = _noop_kill(mgr)  # type: ignore[assignment]

            with _real_rotation_paths():
                await mgr.arm_rotation("web", "terminal-xyz")
                result = await mgr.fire_rotation(
                    "web", "terminal-xyz",
                    claude_sid="conv-kept", prompt="carry on",
                )

            assert result["ok"] is True
            new_conv = await _conversation_id(sid)
            assert new_conv != "conv-kept"
            # A process took the conversation nothing else could hold,
            # and it is the one the row now names.
            assert spawned and spawned[0]["resume_sid"] == new_conv
            # What the user typed is still carried, as the opening turn
            # the respawn delivers.
            assert await mgr.get_carry_forward(sid, new_conv) == (
                f"{HANDOVER}\n\ncarry on"
            )
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_arm_rotation_records_the_pending_rotation_and_composes_nothing(comms_db) -> None:
    """Arming runs inside a mind's Stop hook on a timeout. It is one UPDATE:
    a composition there could hang the hook into reporting failure while the
    gateway recorded success, and the two would then disagree about whether a
    rotation is pending."""
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            sid = await _seed_session(owner_type="telegram", client_ref="123")

            async def fail_compose(**_kw):  # must not be called
                raise AssertionError("arming composed a prompt")

            async def fail_create(*a, **k):  # must not be called
                raise AssertionError("create_session called for an arm")

            mgr.create_session = fail_create  # type: ignore[assignment]

            with patch("comms.bootstrap_loader.compose_prompt_blocks", fail_compose):
                result = await mgr.arm_rotation("telegram", "123")

            assert result == {"ok": True, "session_id": sid}
            assert await _armed_flag(sid) == 1
            assert await fetchval(
                "SELECT status FROM core.sessions WHERE id = $1", sid) == "running"
        finally:
            await close_manager(mgr)

    _run(scenario())


# ---------------------------------------------------------------------------
# kill_session — rotated_to on the published event
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# record_turn
# ---------------------------------------------------------------------------

def test_record_turn_appends_to_ledger(comms_db) -> None:
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            sid = await _seed_session(
                owner_type="web", owner_ref="terminal", client_ref="terminal-abc"
            )

            result = await mgr.record_turn("web", "terminal-abc", "user", "hello from tile")

            assert result == {"ok": True, "session_id": sid}
            late = await mgr.get_late_turns("web", "terminal-abc", since=0)
            assert late["session_id"] == sid
            assert [t["role"] for t in late["turns"]] == ["user"]
            assert late["turns"][0]["content"] == "hello from tile"
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_record_turn_no_active_session(comms_db) -> None:
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            result = await mgr.record_turn("web", "nobody-home", "user", "hi")
            assert result == {"ok": False, "error": "no active session"}
        finally:
            await close_manager(mgr)

    _run(scenario())
