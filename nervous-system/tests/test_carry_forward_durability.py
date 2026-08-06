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
from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from comms.sessions import SessionManager

SEED = "<soul>who you are</soul>\n<recent>what just happened</recent>"


def _run(coro):
    return asyncio.run(coro)


async def _make_manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager()
    await mgr.start()
    return mgr


async def _seed_terminal_session(mgr: SessionManager) -> str:
    """A running browser-terminal session on a conversation about to rotate."""
    session_id = "sess-term"
    now = time.time()
    await mgr._db.execute(
        """INSERT INTO sessions (id, owner_type, owner_ref, model, claude_sid,
                                 created_at, last_active, status, mind_id)
           VALUES (?, 'web', 'terminal', 'opus', 'conv-old', ?, ?, 'running', 'ada')""",
        (session_id, now, now),
    )
    await mgr._db.execute(
        """INSERT INTO active_sessions (client_type, client_ref, session_id)
           VALUES ('web', ?, ?)""",
        (session_id, session_id),
    )
    await mgr._db.commit()
    return session_id


@contextlib.contextmanager
def _a_mind_with_a_live_pane():
    """Composition and the pane respawn stubbed; the row write is the subject."""
    async def mind_row(_db, mind_id):
        return {"name": mind_id, "model": "opus"}

    async def blocks(**_kw):
        return SEED

    async def rotated(_self, **_kw):
        return True

    async def notice(_self, **_kw):
        return True

    with patch("comms.broker.get_mind_by_id", mind_row), \
            patch("comms.bootstrap_loader.compose_prompt_blocks", blocks), \
            patch.object(SessionManager, "_rotate_pty_on_mind", rotated), \
            patch.object(SessionManager, "_pty_notice_on_mind", notice):
        yield


async def _row(mgr: SessionManager, session_id: str) -> dict:
    cur = await mgr._db.execute(
        "SELECT claude_sid, carry_forward, carry_forward_sid FROM sessions WHERE id = ?",
        (session_id,),
    )
    return dict(await cur.fetchone())


def test_rotating_a_terminal_stores_the_carry_forward_on_the_session() -> None:
    """Requirement 5: the composed context lands on the row, keyed to the
    conversation it was composed for."""
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                session_id = await _seed_terminal_session(mgr)
                with _a_mind_with_a_live_pane():
                    result = await mgr.arm_rotation("web", session_id)

                assert result["rotated"] is True
                row = await _row(mgr, session_id)
                assert row["carry_forward"] == SEED
                # Keyed to the *new* conversation — the seed belongs to the
                # thing being started, not the one being left behind.
                assert row["carry_forward_sid"] == row["claude_sid"]
                assert row["claude_sid"] != "conv-old"
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_a_completed_turn_clears_the_stored_carry_forward() -> None:
    """Requirement 7: a recorded turn is the proof the rotation took, so the
    seed stops being replayable from that moment."""
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                session_id = await _seed_terminal_session(mgr)
                with _a_mind_with_a_live_pane():
                    await mgr.arm_rotation("web", session_id)
                sid = (await _row(mgr, session_id))["claude_sid"]
                assert await mgr.get_carry_forward(session_id, sid) == SEED

                await mgr.record_turn("web", session_id, "user", "first thing typed")

                assert await mgr.get_carry_forward(session_id, sid) is None
                assert (await _row(mgr, session_id))["carry_forward"] is None
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_a_carry_forward_is_never_served_to_a_different_conversation() -> None:
    """Requirement 8: a stored seed belongs to one conversation id. Handing it
    to another would graft one conversation's context onto another's."""
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                session_id = await _seed_terminal_session(mgr)
                with _a_mind_with_a_live_pane():
                    await mgr.arm_rotation("web", session_id)
                sid = (await _row(mgr, session_id))["claude_sid"]

                assert await mgr.get_carry_forward(session_id, sid) == SEED
                assert await mgr.get_carry_forward(session_id, "some-other-conv") is None
                assert await mgr.get_carry_forward(session_id, "") is None
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_a_seed_whose_clearing_signal_was_lost_expires_on_its_own() -> None:
    """Requirement 7, the failure path: every clearing signal is
    fire-and-forget, so one dropped POST is the last word. Without an expiry
    the seed stays armed forever and some reattach weeks later replays a dead
    conversation's context over a live one."""
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                session_id = await _seed_terminal_session(mgr)
                with _a_mind_with_a_live_pane():
                    await mgr.arm_rotation("web", session_id)
                sid = (await _row(mgr, session_id))["claude_sid"]
                assert await mgr.get_carry_forward(session_id, sid) == SEED

                # The turn that would have cleared it never arrives — the hook
                # swallowed the failure and its detached child is long gone.
                stale = time.time() - mgr.CARRY_FORWARD_TTL_SECONDS - 60
                await mgr._db.execute(
                    "UPDATE sessions SET carry_forward_at = ? WHERE id = ?",
                    (stale, session_id),
                )
                await mgr._db.commit()

                assert await mgr.get_carry_forward(session_id, sid) is None
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_a_straggling_turn_cannot_clear_a_seed_composed_after_it() -> None:
    """Composition runs for minutes and the user keeps typing through it, so a
    detached Stop child can still be reporting a pre-rotation turn after the
    new seed is written. Naming its conversation is what keeps it from wiping
    a seed it never saw."""
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                session_id = await _seed_terminal_session(mgr)
                with _a_mind_with_a_live_pane():
                    await mgr.arm_rotation("web", session_id)
                sid = (await _row(mgr, session_id))["claude_sid"]

                # The straggler belongs to 'conv-old', the conversation the
                # rotation just replaced.
                await mgr.record_turn("web", session_id, "user", "typed during the window",
                                      claude_sid="conv-old")
                assert await mgr.get_carry_forward(session_id, sid) == SEED

                # A turn in the new conversation does clear it.
                await mgr.record_turn("web", session_id, "user", "first thing typed",
                                      claude_sid=sid)
                assert await mgr.get_carry_forward(session_id, sid) is None
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_a_blank_conversation_id_matches_nothing_even_when_a_row_stores_one() -> None:
    """Requirement 8's edge: a mind that does not know what it is resuming
    must be told nothing. The SQL alone would answer a blank id from a row
    whose stored id is also blank, which is why the guard is not decorative."""
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                session_id = await _seed_terminal_session(mgr)
                await mgr._db.execute(
                    "UPDATE sessions SET carry_forward = ?, carry_forward_sid = '', "
                    "carry_forward_at = ? WHERE id = ?",
                    (SEED, time.time(), session_id),
                )
                await mgr._db.commit()

                assert await mgr.get_carry_forward(session_id, "") is None
                assert await mgr.get_carry_forward(session_id, "   ") is None
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_a_terminal_turn_keeps_the_session_off_the_stale_sweep() -> None:
    """Requirement 1: a browser terminal is not closed by inactivity. Its
    turns are the only thing that can say the session is in use, and the
    sweep must not suspend a row whose pane is still running regardless."""
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                session_id = await _seed_terminal_session(mgr)

                # A turn typed in the pane marks the session as used.
                before = (await _full_row(mgr, session_id))["last_active"]
                await asyncio.sleep(0.01)
                await mgr.record_turn("web", session_id, "user", "still here")
                assert (await _full_row(mgr, session_id))["last_active"] > before

                # And even left untouched past the cutoff, with the row marked
                # idle the way a comms restart marks every row, it survives.
                await mgr._db.execute(
                    "UPDATE sessions SET status = 'idle', last_active = ? WHERE id = ?",
                    (time.time() - mgr.REAP_IDLE_AFTER_SECONDS - 60, session_id),
                )
                await mgr._db.commit()

                assert await mgr.reap_stale_sessions() == []
                assert (await _full_row(mgr, session_id))["status"] == "idle"
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_a_stored_carry_forward_never_rides_out_on_a_session_listing() -> None:
    """Requirement 9: the admin guard on the read route is worth nothing if
    the same blob ships on every listing, which answers to the far weaker
    service token."""
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                session_id = await _seed_terminal_session(mgr)
                with _a_mind_with_a_live_pane():
                    await mgr.arm_rotation("web", session_id)
                assert (await _row(mgr, session_id))["carry_forward"] == SEED

                for listing in (await mgr.list_sessions(),
                                await mgr.list_selectable_sessions("web", "somebody-else")):
                    assert listing, "expected the session to be listed at all"
                    for session in listing:
                        assert "carry_forward" not in session
                        assert "carry_forward_sid" not in session
                        assert "carry_forward_at" not in session
                        assert SEED not in repr(session)
            finally:
                await mgr.shutdown()

    _run(scenario())


async def _full_row(mgr: SessionManager, session_id: str) -> dict:
    cur = await mgr._db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
    return dict(await cur.fetchone())


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    """The comms app with an admin token set, so the guard is live."""
    db_path = tmp_path / "sessions.db"
    monkeypatch.setenv("BROKER_DB_PATH", str(tmp_path / "broker.db"))
    monkeypatch.setenv("SESSIONS_DB_PATH", str(db_path))
    monkeypatch.delenv("COMMS_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-token")

    import importlib

    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app) as client:
        client.sessions_db = str(db_path)
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
    this pins the route onto it, so the handler cannot drop the claude_sid,
    look up the wrong session, or answer without consulting the manager."""
    import sqlite3

    now = time.time()
    with sqlite3.connect(app_client.sessions_db) as db:
        db.execute(
            """INSERT INTO sessions (id, owner_type, owner_ref, model, claude_sid,
                                     created_at, last_active, status, mind_id,
                                     carry_forward, carry_forward_sid, carry_forward_at)
               VALUES ('sess-term', 'web', 'terminal', 'opus', 'conv-new', ?, ?,
                       'running', 'ada', ?, 'conv-new', ?)""",
            (now, now, SEED, now),
        )

    admin = {"Authorization": "Bearer admin-token"}
    base = "/sessions/sess-term/carry-forward"

    owed = app_client.get(f"{base}?claude_sid=conv-new", headers=admin)
    assert owed.status_code == 200
    assert owed.json()["carry_forward"] == SEED

    # The route must carry the conversation id through, not ignore it.
    wrong = app_client.get(f"{base}?claude_sid=conv-other", headers=admin)
    assert wrong.status_code == 200
    assert wrong.json()["carry_forward"] is None
