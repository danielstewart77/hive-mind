"""A conversation's name lives on its session row, and survives a rotation.

Names used to live in the browser terminal's own database, keyed to
`sessions.id` and knowing nothing about a session's lifecycle. A chat rotation
retires the row and mints a new id, so a name given weeks ago sat on a closed
session every picker hides while the conversation carried on under a new id
displaying the first hundred characters of its own first message.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import time
from unittest.mock import patch

import pytest

from comms.sessions import SessionManager


def _run(coro):
    return asyncio.run(coro)


async def _make_manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager()
    await mgr.start()
    return mgr


async def _seed(
    mgr: SessionManager,
    session_id: str,
    *,
    status: str = "running",
    name: str | None = None,
    color: str | None = None,
) -> str:
    now = time.time()
    await mgr._db.execute(
        """INSERT INTO sessions (id, owner_type, owner_ref, model, claude_sid,
                                 created_at, last_active, status, mind_id, name, color)
           VALUES (?, 'telegram', '123', 'opus', ?, ?, ?, ?, 'ada', ?, ?)""",
        (session_id, f"conv-{session_id}", now, now, status, name, color),
    )
    await mgr._db.commit()
    return session_id


@contextlib.contextmanager
def _a_registered_mind(spawns: list):
    """A mind the broker knows about, whose spawn is captured, not performed."""
    async def mind_row(_db, mind_id):
        return {"name": mind_id, "model": "opus"}

    async def blocks(**_kw):
        return "<soul>seed</soul>"

    async def spawn(_self, session_id, spawn_model, **_kw):
        spawns.append((session_id, spawn_model))

    with patch("comms.broker.get_mind_by_id", mind_row), \
            patch("comms.bootstrap_loader.compose_prompt_blocks", blocks), \
            patch.object(SessionManager, "_spawn", spawn):
        yield


# ---------------------------------------------------------------------------
# R2 — one record, and every answer about a session carries it
# ---------------------------------------------------------------------------
def test_a_named_conversation_reports_its_name_on_a_single_session_answer() -> None:
    """R2: the name reaches `get_session`, not just the bulk listing.

    Aimed here on purpose. The listing is a `SELECT *` with a denylist, so a
    new column rides it for free; `_session_dict` is a hand-written allowlist
    and backs every single-session answer — `GET /sessions/{id}`, the
    `/switch` reply, a tile's reattach. A name absent from it is a rename that
    looks right in the picker and still reports "New session" the moment you
    switch to the conversation. Breaks if the column is dropped from that
    allowlist.
    """
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "sess-named")
                await _seed(mgr, "sess-bare")

                await mgr.set_session_name("sess-named", name="Fittimus Maximus")

                assert (await mgr.get_session("sess-named"))["name"] == "Fittimus Maximus"
                assert (await mgr.get_session("sess-bare"))["name"] is None
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_naming_a_conversation_leaves_its_colour_alone() -> None:
    """R2: the write is partial — an absent field means unchanged.

    The route this replaced took the whole record, so the health app, which
    only ever knew about names, blanked the colour chosen at the tile on every
    rename. Breaks if the update goes back to writing both columns
    unconditionally.
    """
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "sess-1")
                await mgr.set_session_name("sess-1", name="Health", color="#5c913b")

                after = await mgr.set_session_name("sess-1", name="Health app")

                assert after["name"] == "Health app"
                assert after["color"] == "#5c913b"
            finally:
                await mgr.shutdown()

    _run(scenario())


# ---------------------------------------------------------------------------
# R3 — a rotation successor is born wearing the name
# ---------------------------------------------------------------------------
def test_a_rotation_successor_inherits_the_name_it_continued() -> None:
    """R3: the successor `create_session` mints carries the name and colour.

    Breaks if the inheritance copy is dropped — which is the live bug: three
    rotations of one conversation produced three rows and the name stayed on
    the first.
    """
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "sess-old")
                await mgr.set_session_name(
                    "sess-old", name="Fittimus Maximus", color="#3481cc"
                )

                with _a_registered_mind([]):
                    successor = await mgr.create_session(
                        owner_type="telegram",
                        owner_ref="123",
                        client_ref="123",
                        mind_id="ada",
                        rotated_from="sess-old",
                    )

                assert successor["id"] != "sess-old"
                assert successor["name"] == "Fittimus Maximus"
                assert successor["color"] == "#3481cc"
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_a_fresh_conversation_is_not_named_by_its_neighbour() -> None:
    """R3: inheritance follows the rotation link and nothing else.

    A session created with no predecessor starts unnamed even while a named
    conversation exists on the same mind and surface. Breaks if the copy is
    ever widened to "the most recent name on this mind", which is the shape a
    guess would take.
    """
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "sess-old")
                await mgr.set_session_name("sess-old", name="Fittimus Maximus")

                with _a_registered_mind([]):
                    fresh = await mgr.create_session(
                        owner_type="telegram",
                        owner_ref="123",
                        client_ref="456",
                        mind_id="ada",
                    )

                assert fresh["name"] is None
            finally:
                await mgr.shutdown()

    _run(scenario())


# ---------------------------------------------------------------------------
# R5 — clearing
# ---------------------------------------------------------------------------
def test_clearing_a_name_leaves_the_conversation_holding_none() -> None:
    """R5: an empty name removes it rather than storing an empty label.

    Breaks if the clear writes `''`, which every display then draws as a name
    and no picker falls back from.
    """
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "sess-1")
                await mgr.set_session_name("sess-1", name="Temporary")

                cleared = await mgr.set_session_name("sess-1", name="")

                assert cleared["name"] is None
            finally:
                await mgr.shutdown()

    _run(scenario())


# ---------------------------------------------------------------------------
# The colour check, which used to live in the route being deleted
# ---------------------------------------------------------------------------
def test_a_colour_that_is_not_a_hex_value_is_refused() -> None:
    """The stored colour is assigned into a style attribute by the browser.

    The only validation anywhere lived in the terminal route this change
    replaces, so it had to move with the write rather than die with its old
    home. Breaks if the check is dropped.
    """
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "sess-1")

                accepted = await mgr.set_session_name("sess-1", color="#5c913b")
                assert accepted["color"] == "#5c913b"

                with pytest.raises(ValueError):
                    await mgr.set_session_name("sess-1", color="red; content:url(x)")

                still = await mgr.get_session("sess-1")
                assert still["color"] == "#5c913b"
            finally:
                await mgr.shutdown()

    _run(scenario())


# ---------------------------------------------------------------------------
# A rename addressed to a conversation that has already rotated away
# ---------------------------------------------------------------------------
def test_a_rename_on_a_retired_conversation_is_refused() -> None:
    """Tonight's bug, one layer in: the closed row is still writable.

    A picker button and a rename prompt both carry the id captured when they
    were drawn, and neither expires. Answer a day-old prompt after the
    conversation rotated and the write would land on a row nothing reads, and
    report success. Breaks if the status check is removed — the same write
    against a live row must still succeed, or the test is only proving that
    renaming is broken.
    """
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "sess-live", status="running")
                await _seed(mgr, "sess-retired", status="closed")

                assert (await mgr.set_session_name(
                    "sess-live", name="Health"
                ))["name"] == "Health"

                with pytest.raises(PermissionError):
                    await mgr.set_session_name("sess-retired", name="Health")

                cur = await mgr._db.execute(
                    "SELECT name FROM sessions WHERE id = 'sess-retired'"
                )
                assert (await cur.fetchone())["name"] is None
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_a_name_for_a_session_that_does_not_exist_is_refused() -> None:
    """A rename must not create the conversation it names.

    The failing id is sourced from the store rather than invented: it is read
    back after the row is gone, so it is provably absent rather than merely
    odd-looking. Breaks if the write becomes an upsert.
    """
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "sess-1")
                await mgr._db.execute("DELETE FROM sessions WHERE id = 'sess-1'")
                await mgr._db.commit()

                with pytest.raises(LookupError):
                    await mgr.set_session_name("sess-1", name="Ghost")

                cur = await mgr._db.execute("SELECT COUNT(*) c FROM sessions")
                assert (await cur.fetchone())["c"] == 0
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_a_rename_by_short_id_lands_on_the_conversation_it_resolved() -> None:
    """A write that reports success must have changed something.

    Short ids resolve by prefix, and the update used to be addressed with the
    argument rather than the id it resolved to — matching zero rows, committing,
    and answering 200 with the row's old name. Breaks if the write goes back to
    the raw argument.
    """
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "abcdef12-3456-7890-abcd-ef1234567890")

                # The prefix is sourced from the id the store actually holds,
                # so it is provably a resolvable short form rather than a
                # hand-typed guess.
                answered = await mgr.set_session_name("abcdef12", name="Health")

                assert answered["name"] == "Health"
                cur = await mgr._db.execute(
                    "SELECT name FROM sessions WHERE id = "
                    "'abcdef12-3456-7890-abcd-ef1234567890'"
                )
                assert (await cur.fetchone())["name"] == "Health"
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_a_name_on_an_ended_conversation_can_still_be_cleared() -> None:
    """Ended conversations keep their names, so the button that removes one works.

    Nothing strips a name on close and surfaces showing history draw it, so
    refusing every write to a closed row would leave a name on screen whose
    clear button answered 409 forever. Naming one is still refused — the two
    are not the same act. Breaks if the status check stops distinguishing them.
    """
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "sess-done", status="closed", name="Old work")

                cleared = await mgr.set_session_name("sess-done", name="")
                assert cleared["name"] is None

                with pytest.raises(PermissionError):
                    await mgr.set_session_name("sess-done", name="Named again")
            finally:
                await mgr.shutdown()

    _run(scenario())
