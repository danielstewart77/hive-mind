"""A rotation's carry-forward outlives the process it was composed for.

A terminal rotation hands the new harness its carry-forward as a system
prompt on the respawned pane, and that is the only place it ever lived: the
seed file is deleted the instant the pane reads it, and a system prompt
never reaches the transcript. So the context survived exactly as long as
that one process. Kill the pane before the user types — an idle reaper, a
service restart, a crash — and reattaching resumed a conversation id with no
transcript behind it and no seed left to re-apply. The user came back to an
empty terminal holding none of what the rotation had just spent minutes
composing.

The session row outlives every process bound to it, so that is where the
carry-forward belongs. It is written with the new conversation id, served
back only to a caller asking about *that* conversation, and cleared by the
first completed turn — which is the only evidence the rotation actually
took.
"""
# ruff: noqa: F811 - pytest fixtures are imported, then named as parameters
from __future__ import annotations

import asyncio
import contextlib
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from comms.sessions import SessionManager
from comms_pg_fixture import (  # noqa: F401 - fixtures
    close_manager,
    comms_db,
    execute,
    fetchrow,
    make_manager,
    register_mind,
    scratch_dsn,
    seed_session,
)

SEED = "<soul>who you are</soul>\n<recent>what just happened</recent>"
#: What the mind renders of the conversation being replaced. A pane opens on
#: this as its user turn, with the soul beside it as a system prompt.
HANDOVER = "here is what came before"


#: These tests are about their own subject, not about authorization, so they
#: present the admin credential. They used to clear every token instead and
#: rely on the gateway serving open when unconfigured — a bypass that is gone,
#: because a gateway that cannot establish who is calling must refuse rather
#: than answer the LAN.
GRILL_ADMIN = "admin-token"  # secret-guard: allow


def _run(coro):
    return asyncio.run(coro)


async def _seed_terminal_session() -> str:
    """A running browser-terminal session on a conversation about to rotate."""
    session_id = "sess-term"
    await register_mind("ada", model="opus")
    await seed_session(
        session_id, surface="web", surface_id="terminal", model="opus",
        conversation_id="conv-old", terminal_held=True, bind=("web", session_id),
    )
    return session_id


@contextlib.contextmanager
def _a_mind_with_a_live_pane():
    """Composition and the pane respawn stubbed; the row write is the subject."""
    async def blocks(**_kw):
        return SEED

    async def rotated(_self, **_kw):
        return True

    async def handover(_self, *_a, **_kw):
        return HANDOVER

    with patch("comms.bootstrap_loader.compose_prompt_blocks", blocks), \
            patch.object(SessionManager, "_render_handover_on_mind", handover), \
            patch.object(SessionManager, "_clear_rotation_marker_on_mind",
                         lambda *_a, **_kw: asyncio.sleep(0)), \
            patch.object(SessionManager, "_rotate_pty_on_mind", rotated):
        yield


async def _row(session_id: str) -> dict:
    return await fetchrow(
        "SELECT conversation_id AS conversation_id, carry_forward, carry_forward_sid "
        "FROM core.sessions WHERE id = $1",
        session_id,
    )


async def _rotate(mgr: SessionManager, session_id: str, prompt: str = "go on") -> dict:
    """Take a terminal all the way through a rotation.

    Arming records only that a rotation is pending; the pane's own typed turn
    is what replaces the conversation. Every case below is about the seed, so
    both halves run and the assertions stay on what the row holds afterwards.
    """
    await mgr.arm_rotation("web", session_id)
    return await mgr.fire_rotation(
        "web", session_id, conversation_id="conv-old", prompt=prompt,
    )


def _delivered(prompt: str = "go on") -> str:
    """What the pane is actually handed: the handover and the typed message as
    one opening turn. The row stores this, not the handover alone — a tile
    that dies before the first turn completes reattaches asking for it, and
    the handover without the message loses what the user typed."""
    return f"{HANDOVER}\n\n{prompt}"


def test_rotating_a_terminal_stores_the_carry_forward_on_the_session(comms_db) -> None:
    """Requirement 5: the composed context lands on the row, keyed to the
    conversation it was composed for."""
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            session_id = await _seed_terminal_session()
            with _a_mind_with_a_live_pane():
                result = await _rotate(mgr, session_id)

            assert result["rotated"] is True
            row = await _row(session_id)
            assert row["carry_forward"] == _delivered()
            # Keyed to the *new* conversation — the seed belongs to the
            # thing being started, not the one being left behind.
            assert row["carry_forward_sid"] == row["conversation_id"]
            assert row["conversation_id"] != "conv-old"
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_a_completed_turn_clears_the_stored_carry_forward(comms_db) -> None:
    """Requirement 7: a recorded turn is the proof the rotation took, so the
    seed stops being replayable from that moment."""
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            session_id = await _seed_terminal_session()
            with _a_mind_with_a_live_pane():
                await _rotate(mgr, session_id)
            sid = (await _row(session_id))["conversation_id"]
            assert await mgr.get_carry_forward(session_id, sid) == _delivered()

            await mgr.record_turn("web", session_id, "user", "first thing typed")

            assert await mgr.get_carry_forward(session_id, sid) is None
            assert (await _row(session_id))["carry_forward"] is None
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_a_carry_forward_is_never_served_to_a_different_conversation(comms_db) -> None:
    """Requirement 8: a stored seed belongs to one conversation id. Handing it
    to another would graft one conversation's context onto another's."""
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            session_id = await _seed_terminal_session()
            with _a_mind_with_a_live_pane():
                await _rotate(mgr, session_id)
            sid = (await _row(session_id))["conversation_id"]

            assert await mgr.get_carry_forward(session_id, sid) == _delivered()
            assert await mgr.get_carry_forward(session_id, "some-other-conv") is None
            assert await mgr.get_carry_forward(session_id, "") is None
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_a_seed_whose_clearing_signal_was_lost_expires_on_its_own(comms_db) -> None:
    """Requirement 7, the failure path: every clearing signal is
    fire-and-forget, so one dropped POST is the last word. Without an expiry
    the seed stays armed forever and some reattach weeks later replays a dead
    conversation's context over a live one."""
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            session_id = await _seed_terminal_session()
            with _a_mind_with_a_live_pane():
                await _rotate(mgr, session_id)
            sid = (await _row(session_id))["conversation_id"]
            assert await mgr.get_carry_forward(session_id, sid) == _delivered()

            # The turn that would have cleared it never arrives — the hook
            # swallowed the failure and its detached child is long gone.
            stale = time.time() - mgr.CARRY_FORWARD_TTL_SECONDS - 60
            await execute(
                "UPDATE core.sessions SET carry_forward_at = $1 WHERE id = $2",
                stale, session_id,
            )

            assert await mgr.get_carry_forward(session_id, sid) is None
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_a_straggling_turn_cannot_clear_a_seed_composed_after_it(comms_db) -> None:
    """Composition runs for minutes and the user keeps typing through it, so a
    detached Stop child can still be reporting a pre-rotation turn after the
    new seed is written. Naming its conversation is what keeps it from wiping
    a seed it never saw."""
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            session_id = await _seed_terminal_session()
            with _a_mind_with_a_live_pane():
                await _rotate(mgr, session_id)
            sid = (await _row(session_id))["conversation_id"]

            # The straggler belongs to 'conv-old', the conversation the
            # rotation just replaced.
            await mgr.record_turn("web", session_id, "user", "typed during the window",
                                  conversation_id="conv-old")
            assert await mgr.get_carry_forward(session_id, sid) == _delivered()

            # A turn in the new conversation does clear it.
            await mgr.record_turn("web", session_id, "user", "first thing typed",
                                  conversation_id=sid)
            assert await mgr.get_carry_forward(session_id, sid) is None
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_a_blank_conversation_id_matches_nothing_even_when_a_row_stores_one(comms_db) -> None:
    """Requirement 8's edge: a mind that does not know what it is resuming
    must be told nothing. The SQL alone would answer a blank id from a row
    whose stored id is also blank, which is why the guard is not decorative."""
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            session_id = await _seed_terminal_session()
            await execute(
                "UPDATE core.sessions SET carry_forward = $1, carry_forward_sid = '', "
                "carry_forward_at = $2 WHERE id = $3",
                SEED, time.time(), session_id,
            )

            assert await mgr.get_carry_forward(session_id, "") is None
            assert await mgr.get_carry_forward(session_id, "   ") is None
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_a_terminal_turn_marks_the_session_as_used(comms_db) -> None:
    """Requirement 1: a turn typed in a pane is the only write a pty turn
    makes, and it is what keeps `last_active` current. Nothing else on the
    terminal path touches the row, so if this stops updating, the session's
    last-used time is frozen at whenever it was created."""
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            session_id = await _seed_terminal_session()

            before = (await _full_row(session_id))["last_active"]
            await asyncio.sleep(0.01)
            await mgr.record_turn("web", session_id, "user", "still here")
            after = (await _full_row(session_id))["last_active"]

            assert after > before
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_a_stored_carry_forward_never_rides_out_on_a_session_listing(comms_db) -> None:
    """Requirement 9: the admin guard on the read route is worth nothing if
    the same blob ships on every listing, which answers to the far weaker
    service token."""
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            session_id = await _seed_terminal_session()
            with _a_mind_with_a_live_pane():
                await _rotate(mgr, session_id)
            assert (await _row(session_id))["carry_forward"] == _delivered()

            for listing in (await mgr.list_sessions(),
                            await mgr.list_selectable_sessions("web", "somebody-else")):
                assert listing, "expected the session to be listed at all"
                for session in listing:
                    assert "carry_forward" not in session
                    assert "carry_forward_sid" not in session
                    assert "carry_forward_at" not in session
                    assert SEED not in repr(session)
        finally:
            await close_manager(mgr)

    _run(scenario())


async def _full_row(session_id: str) -> dict:
    return await fetchrow("SELECT * FROM core.sessions WHERE id = $1", session_id)


@pytest.fixture
def app_client(monkeypatch, comms_db):
    """The comms app with an admin token set, so the guard is live."""
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-token")

    import importlib

    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app) as client:
        yield client


def test_reading_a_carry_forward_requires_the_admin_credential(app_client) -> None:
    """Requirement 9: the stored blob is the mind's soul, its recent memory
    and the last exchange, on a port reachable across the LAN."""
    path = "/sessions/sess-term/carry-forward"

    assert app_client.get(path).status_code == 401
    assert app_client.get(
        path,
        headers={"Authorization": "Bearer not-the-admin-token"},  # secret-guard: allow
    ).status_code == 401

    allowed = app_client.get(path, headers={"Authorization": "Bearer admin-token"})
    assert allowed.status_code == 200
    assert allowed.json()["carry_forward"] is None


def test_the_route_serves_the_stored_seed_to_the_conversation_it_belongs_to(
    app_client,
) -> None:
    """Requirement 6 end to end. The manager is tested directly elsewhere;
    this pins the route onto it, so the handler cannot drop the conversation_id,
    look up the wrong session, or answer without consulting the manager."""
    now = time.time()

    # Plain connections only: the app's pool belongs to the client's own
    # event loop, and this seed runs on another.
    async def seed() -> None:
        await execute(
            "INSERT INTO core.minds (mind_id, name, gateway_url, model, harness, "
            "registered_at, last_seen) VALUES ('ada', 'ada', 'http://mind.invalid', "
            "'opus', 'claude_cli', $1, $1) ON CONFLICT (mind_id) DO NOTHING", now,
        )
        await execute(
            "INSERT INTO core.sessions (id, surface, surface_id, model, "
            "conversation_id, created_at, last_active, status, mind_id, "
            "carry_forward, carry_forward_sid, carry_forward_at) VALUES "
            "('sess-term', 'web', 'terminal', 'opus', 'conv-new', $1, $1, "
            "'running', 'ada', $2, 'conv-new', $1)", now, SEED,
        )

    asyncio.run(seed())

    admin = {"Authorization": "Bearer admin-token"}
    base = "/sessions/sess-term/carry-forward"

    owed = app_client.get(f"{base}?conversation_id=conv-new", headers=admin)
    assert owed.status_code == 200
    assert owed.json()["carry_forward"] == SEED

    # The route must carry the conversation id through, not ignore it.
    wrong = app_client.get(f"{base}?conversation_id=conv-other", headers=admin)
    assert wrong.status_code == 200
    assert wrong.json()["carry_forward"] is None
