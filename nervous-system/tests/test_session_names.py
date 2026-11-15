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
from unittest.mock import patch

import pytest

from comms.sessions import SessionManager
from comms_pg_fixture import (  # noqa: F401 - fixtures
    close_manager,
    comms_db,
    execute,
    fetchval,
    make_manager,
    register_mind,
    scratch_dsn,
    seed_session,
)


def _run(coro):
    return asyncio.run(coro)


async def _make_manager() -> SessionManager:
    mgr = await make_manager()
    return mgr


async def _seed(
    mgr: SessionManager,
    session_id: str,
    *,
    status: str = "running",
    name: str | None = None,
    color: str | None = None,
) -> str:
    await seed_session(
        session_id, surface="telegram", surface_id="123", model="opus",
        conversation_id=f"conv-{session_id}", status=status, mind_id="ada",
        name=name, color=color,
    )
    return session_id


@contextlib.contextmanager
def _a_registered_mind(spawns: list):
    """Prompt composition and the spawn stubbed: the transport to lucent and
    to the mind. Register the mind itself with `register_mind` first."""
    async def blocks(**_kw):
        return "<soul>seed</soul>"

    async def spawn(_self, session_id, spawn_model, **_kw):
        spawns.append((session_id, spawn_model))

    with patch("comms.bootstrap_loader.compose_prompt_blocks", blocks), \
            patch.object(SessionManager, "_spawn", spawn):
        yield


# ---------------------------------------------------------------------------
# R2 — one record, and every answer about a session carries it
# ---------------------------------------------------------------------------
def test_a_named_conversation_reports_its_name_on_a_single_session_answer(comms_db) -> None:
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
        mgr = await _make_manager()
        try:
            await _seed(mgr, "sess-named")
            await _seed(mgr, "sess-bare")

            await mgr.set_session_name("sess-named", name="Fittimus Maximus")

            assert (await mgr.get_session("sess-named"))["name"] == "Fittimus Maximus"
            assert (await mgr.get_session("sess-bare"))["name"] is None
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_naming_a_conversation_leaves_its_colour_alone(comms_db) -> None:
    """R2: the write is partial — an absent field means unchanged.

    The route this replaced took the whole record, so the health app, which
    only ever knew about names, blanked the colour chosen at the tile on every
    rename. Breaks if the update goes back to writing both columns
    unconditionally.
    """
    async def scenario() -> None:
        mgr = await _make_manager()
        try:
            await _seed(mgr, "sess-1")
            await mgr.set_session_name("sess-1", name="Health", color="#5c913b")

            after = await mgr.set_session_name("sess-1", name="Health app")

            assert after["name"] == "Health app"
            assert after["color"] == "#5c913b"
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_a_fresh_conversation_is_not_named_by_its_neighbour(comms_db) -> None:
    """R3: inheritance follows the rotation link and nothing else.

    A session created with no predecessor starts unnamed even while a named
    conversation exists on the same mind and surface. Breaks if the copy is
    ever widened to "the most recent name on this mind", which is the shape a
    guess would take.
    """
    async def scenario() -> None:
        mgr = await _make_manager()
        try:
            await _seed(mgr, "sess-old")
            await mgr.set_session_name("sess-old", name="Fittimus Maximus")

            # Declares no harness, so no harness listing is asked of it.
            await register_mind("ada", model="opus", harness="")
            with _a_registered_mind([]):
                fresh = await mgr.create_session(
                    surface="telegram",
                    surface_id="123",
                    client_ref="456",
                    mind_id="ada",
                )

            assert fresh["name"] is None
        finally:
            await close_manager(mgr)

    _run(scenario())


# ---------------------------------------------------------------------------
# R5 — clearing
# ---------------------------------------------------------------------------
def test_clearing_a_name_leaves_the_conversation_holding_none(comms_db) -> None:
    """R5: an empty name removes it rather than storing an empty label.

    Breaks if the clear writes `''`, which every display then draws as a name
    and no picker falls back from.
    """
    async def scenario() -> None:
        mgr = await _make_manager()
        try:
            await _seed(mgr, "sess-1")
            await mgr.set_session_name("sess-1", name="Temporary")

            cleared = await mgr.set_session_name("sess-1", name="")

            assert cleared["name"] is None
        finally:
            await close_manager(mgr)

    _run(scenario())


# ---------------------------------------------------------------------------
# The colour check, which used to live in the route being deleted
# ---------------------------------------------------------------------------
def test_a_colour_that_is_not_a_hex_value_is_refused(comms_db) -> None:
    """The stored colour is assigned into a style attribute by the browser.

    The only validation anywhere lived in the terminal route this change
    replaces, so it had to move with the write rather than die with its old
    home. Breaks if the check is dropped.
    """
    async def scenario() -> None:
        mgr = await _make_manager()
        try:
            await _seed(mgr, "sess-1")

            accepted = await mgr.set_session_name("sess-1", color="#5c913b")
            assert accepted["color"] == "#5c913b"

            with pytest.raises(ValueError):
                await mgr.set_session_name("sess-1", color="red; content:url(x)")

            still = await mgr.get_session("sess-1")
            assert still["color"] == "#5c913b"
        finally:
            await close_manager(mgr)

    _run(scenario())


# ---------------------------------------------------------------------------
# A rename addressed to a conversation that has already rotated away
# ---------------------------------------------------------------------------
def test_a_rename_on_a_retired_conversation_is_refused(comms_db) -> None:
    """Tonight's bug, one layer in: the closed row is still writable.

    A picker button and a rename prompt both carry the id captured when they
    were drawn, and neither expires. Answer a day-old prompt after the
    conversation rotated and the write would land on a row nothing reads, and
    report success. Breaks if the status check is removed — the same write
    against a live row must still succeed, or the test is only proving that
    renaming is broken.
    """
    async def scenario() -> None:
        mgr = await _make_manager()
        try:
            await _seed(mgr, "sess-live", status="running")
            await _seed(mgr, "sess-retired", status="closed")

            assert (await mgr.set_session_name(
                "sess-live", name="Health"
            ))["name"] == "Health"

            with pytest.raises(PermissionError):
                await mgr.set_session_name("sess-retired", name="Health")

            assert await fetchval(
                "SELECT name FROM core.sessions WHERE id = 'sess-retired'"
            ) is None
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_a_name_for_a_session_that_does_not_exist_is_refused(comms_db) -> None:
    """A rename must not create the conversation it names.

    The failing id is sourced from the store rather than invented: it is read
    back after the row is gone, so it is provably absent rather than merely
    odd-looking. Breaks if the write becomes an upsert.
    """
    async def scenario() -> None:
        mgr = await _make_manager()
        try:
            await _seed(mgr, "sess-1")
            await execute("DELETE FROM core.sessions WHERE id = 'sess-1'")

            with pytest.raises(LookupError):
                await mgr.set_session_name("sess-1", name="Ghost")

            assert await fetchval("SELECT COUNT(*) FROM core.sessions") == 0
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_a_rename_by_short_id_lands_on_the_conversation_it_resolved(comms_db) -> None:
    """A write that reports success must have changed something.

    Short ids resolve by prefix, and the update used to be addressed with the
    argument rather than the id it resolved to — matching zero rows, committing,
    and answering 200 with the row's old name. Breaks if the write goes back to
    the raw argument.
    """
    async def scenario() -> None:
        mgr = await _make_manager()
        try:
            await _seed(mgr, "abcdef12-3456-7890-abcd-ef1234567890")

            # The prefix is sourced from the id the store actually holds,
            # so it is provably a resolvable short form rather than a
            # hand-typed guess.
            answered = await mgr.set_session_name("abcdef12", name="Health")

            assert answered["name"] == "Health"
            assert await fetchval(
                "SELECT name FROM core.sessions WHERE id = "
                "'abcdef12-3456-7890-abcd-ef1234567890'"
            ) == "Health"
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_a_name_on_an_ended_conversation_can_still_be_cleared(comms_db) -> None:
    """Ended conversations keep their names, so the button that removes one works.

    Nothing strips a name on close and surfaces showing history draw it, so
    refusing every write to a closed row would leave a name on screen whose
    clear button answered 409 forever. Naming one is still refused — the two
    are not the same act. Breaks if the status check stops distinguishing them.
    """
    async def scenario() -> None:
        mgr = await _make_manager()
        try:
            await _seed(mgr, "sess-done", status="closed", name="Old work")

            cleared = await mgr.set_session_name("sess-done", name="")
            assert cleared["name"] is None

            with pytest.raises(PermissionError):
                await mgr.set_session_name("sess-done", name="Named again")
        finally:
            await close_manager(mgr)

    _run(scenario())


# ---------------------------------------------------------------------------
# A name arrives with the conversation, not by a second call
# ---------------------------------------------------------------------------
def test_a_conversation_created_with_a_name_holds_it_from_birth(comms_db) -> None:
    """R2, R4: the create stamps the label, and every reader of one sees it.

    A second call is a thing that gets skipped, refused, or lost with the turn
    that was going to make it — and until it lands every picker draws the
    conversation as the mind's own name, which is the bug this exists to close.
    Asserted on all three readers a surface actually uses: the create's own
    answer, the stored column, and the bulk `session_names` read the terminal's
    rail is drawn from. Breaks if the columns stop riding the insert, or if
    `name` is dropped from `_session_dict`'s hand-written allowlist.
    """
    async def scenario() -> None:
        mgr = await _make_manager()
        try:
            await register_mind("ada", model="opus", harness="")
            with _a_registered_mind([]):
                named = await mgr.create_session(
                    surface="web", surface_id="terminal", client_ref="staff-1",
                    mind_id="ada", name="  CoS: Session names  ",
                    color="#5c913b",
                )
                bare = await mgr.create_session(
                    surface="web", surface_id="terminal", client_ref="staff-2",
                    mind_id="ada",
                )

            assert named["name"] == "CoS: Session names"
            assert named["color"] == "#5c913b"
            assert await fetchval(
                "SELECT name FROM core.sessions WHERE id = $1", named["id"]
            ) == "CoS: Session names"

            labels = await mgr.session_names()
            assert labels[named["id"]]["name"] == "CoS: Session names"
            assert labels[named["id"]]["color"] == "#5c913b"

            # The create that names nothing is the one every surface bot
            # already makes, and it has to come out exactly as it did.
            assert bare["name"] is None
            assert bare["color"] is None
            assert bare["id"] not in labels

            # Empty is not a short name, it is no name — so a conversation
            # created with one is indistinguishable from one created with
            # none, and the rail falls back rather than drawing a blank tile.
            with _a_registered_mind([]):
                blank = await mgr.create_session(
                    surface="web", surface_id="terminal", client_ref="staff-3",
                    mind_id="ada", name="   ",
                )
            assert blank["name"] is None
            assert await fetchval(
                "SELECT name FROM core.sessions WHERE id = $1", blank["id"]
            ) is None
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_a_name_is_normalised_the_same_at_creation_as_at_a_rename(comms_db) -> None:
    """R2: one normaliser, so the two writes cannot drift apart.

    The trim and the forty-character cap were an inline expression in
    `set_session_name` while renaming was the only way a name could arrive. A
    copy of that expression in the create passes every test on the day it is
    written and drifts later — a name stored at a length a rename would have
    cut, with nothing failing. So the two paths are given the same awkward
    input and their stored values compared to each other rather than to a
    literal. Breaks when the two paths normalise *differently* — which is the
    whole of what a behavioural test can see here. An exact copy of the
    expression in either path passes this and always will; nothing short of
    reading the code catches that, and it is not what drift looks like.
    """
    async def scenario() -> None:
        mgr = await _make_manager()
        try:
            long_name = "  " + "Re-index every lucent collection on the hive" + "  "

            await _seed(mgr, "sess-renamed")
            renamed = await mgr.set_session_name("sess-renamed", name=long_name)

            await register_mind("ada", model="opus", harness="")
            with _a_registered_mind([]):
                created = await mgr.create_session(
                    surface="web", surface_id="terminal", client_ref="staff-1",
                    mind_id="ada", name=long_name,
                )

            assert created["name"] == renamed["name"]
            # Sourced from the input rather than stated as a number: the test
            # must fail if the cap moves in one path only, not if it moves.
            assert len(created["name"]) < len(long_name.strip())
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_a_colour_that_is_not_a_hex_value_creates_no_conversation(comms_db) -> None:
    """R3: refused before anything is written, not after.

    The row and its surface binding go in one unit of work and the harness is
    spawned after it, so a colour checked any later would have to be refused
    by undoing a conversation that already exists — and the binding it
    replaced is not coming back. The valid colour is created in the same test
    so that a create which simply never works cannot pass it. Breaks if the
    check moves below `_atomically(_write)` or is dropped.
    """
    async def scenario() -> None:
        mgr = await _make_manager()
        try:
            await register_mind("ada", model="opus", harness="")
            with _a_registered_mind([]):
                accepted = await mgr.create_session(
                    surface="web", surface_id="terminal", client_ref="staff-1",
                    mind_id="ada", color="#5c913b",
                )
                assert accepted["color"] == "#5c913b"

                with pytest.raises(ValueError):
                    await mgr.create_session(
                        surface="web", surface_id="terminal",
                        client_ref="staff-2", mind_id="ada",
                        color="red; content:url(x)",
                    )

            assert await fetchval("SELECT COUNT(*) FROM core.sessions") == 1
            assert await fetchval(
                "SELECT COUNT(*) FROM core.active_sessions "
                "WHERE client_ref = 'staff-2'"
            ) == 0
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_a_name_that_cannot_be_stored_costs_the_label_and_not_the_conversation(
    comms_db,
) -> None:
    """R2: a label is a label, and losing one must not lose the conversation.

    Two characters would otherwise take the whole create down with them: a NUL,
    which a Postgres `text` column refuses outright, and an unpaired surrogate,
    which no UTF-8 encoder will accept. The name rides the same unit of work as
    the session row and its binding, so either one rolls back the create and
    the caller is told the mind is unreachable. Breaks if the laundering leaves
    `normalised_name` — and it breaks loudly, because the refusal comes from
    the driver and not from this module.
    """
    async def scenario() -> None:
        mgr = await _make_manager()
        try:
            await register_mind("ada", model="opus", harness="")
            with _a_registered_mind([]):
                created = await mgr.create_session(
                    surface="web", surface_id="terminal", client_ref="staff-1",
                    mind_id="ada", name="CoS: re\x00index \ud800lucent",
                )

            stored = await fetchval(
                "SELECT name FROM core.sessions WHERE id = $1", created["id"]
            )
            assert created["id"]
            assert stored == created["name"]
            assert "\x00" not in stored
            assert stored.startswith("CoS: re")
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_renaming_a_conversation_named_at_creation_leaves_its_colour_alone(
    comms_db,
) -> None:
    """R5: the partial update still holds over a colour set at creation.

    The rename route takes only the fields it was sent, and the colour it must
    not touch now has a second way of getting onto the row. Breaks if the
    create fails to store the colour, or if the rename goes back to writing
    both columns unconditionally.
    """
    async def scenario() -> None:
        mgr = await _make_manager()
        try:
            await register_mind("ada", model="opus", harness="")
            with _a_registered_mind([]):
                created = await mgr.create_session(
                    surface="web", surface_id="terminal", client_ref="staff-1",
                    mind_id="ada", name="CoS: Health", color="#5c913b",
                )

            after = await mgr.set_session_name(created["id"], name="CoS: Health app")

            assert after["name"] == "CoS: Health app"
            assert after["color"] == "#5c913b"
        finally:
            await close_manager(mgr)

    _run(scenario())
