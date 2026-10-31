"""The behaviours the console depends on, through the `board` operations.

Every assertion reads the row back with a plain driver connection rather
than through a second operation, for the reason the round-trip suite gives:
a layer that writes and reads the same wrong column consistently passes its
own round trip.

The board these tests work on is built here and written into the real
Postgres the rest of the suite uses. Built rather than copied from this
host's live board on purpose: a suite that needs one machine's rows to be
present is a suite that fails on every other machine, and the shape the
numbering has to get right - an epic holding both features and a story of
its own, three levels deep - is a shape a test can state and a live board
can only happen to contain.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

import hive_data

from hive_data import board
from hive_data.errors import MissingReference, TransactionMisuse, ValueRefused


def _id(prefix: str) -> str:
    """An id of this test's own.

    The scratch database is made once per run, so rows outlive the test that
    wrote them - and `work_items` is read whole by the numbering, so a test
    sharing an id with another would be numbering somebody else's board.
    """
    return f"{prefix}{uuid.uuid4().hex[:12]}"


class Deliberate(Exception):
    """Raised to roll a unit of work back on purpose."""


async def _board(*, epic_at: int = 1) -> dict[str, str]:
    """One epic, two features with two stories each, and a loose story.

    Positions are given explicitly, one per sibling, so every ordering
    assertion below is about the order these rows were given rather than
    about the order they happened to be inserted in.
    """
    ids = {
        "epic": _id("e"),
        "feature_one": _id("f"), "feature_two": _id("f"),
        "story_one": _id("s"), "story_two": _id("s"),
        "story_three": _id("s"), "story_four": _id("s"),
        "loose": _id("s"),
    }
    async with hive_data.transaction() as unit:
        await board.create_work_item(
            ids["epic"], "epic", "the epic", epic_at, unit=unit)
        for index, key in enumerate(("feature_one", "feature_two"), start=1):
            await board.create_work_item(
                ids[key], "feature", f"feature {index}", index,
                parent_id=ids["epic"], unit=unit)
        pairs = (("story_one", "feature_one", 1), ("story_two",
                 "feature_one", 2), ("story_three", "feature_two", 1),
                 ("story_four", "feature_two", 2))
        for key, parent, position in pairs:
            await board.create_work_item(
                ids[key], "story", key, position, parent_id=ids[parent],
                status="backlog", unit=unit)
        # A story hanging directly off the epic, beside the two features.
        # This is the row that makes the numbering a tree rather than three
        # flat lists, and the live board has rows like it.
        await board.create_work_item(
            ids["loose"], "story", "a loose story", 3,
            parent_id=ids["epic"], status="backlog", unit=unit)
    return ids


@pytest.fixture
def a_notice_time():
    """A fixed moment, so no notice ordering depends on when a test ran."""
    return 1_800_000_000.0


# --------------------------------------------------------------- reachability

async def test_every_board_table_returns_what_was_written(
        connected, inspector, a_notice_time, a_mind):
    """Requirement one, and the only thing that proves a table is actually
    reachable: a value put in through the layer comes back out of it."""
    now = a_notice_time
    item_id = _id("e")
    design_id = _id("d")
    node_id = _id("node-")
    mind_name = _id("mind-")

    await board.create_work_item(
        item_id, "epic", "an epic", 7, description="why",
        requirements="one", acceptance_criteria="two", qa_tests="three")
    await board.create_design_session(
        design_id, "a design", now, now, mind_id=a_mind, repos='["hive"]')
    queued = await board.queue_notice(
        design_id, "submitted", "requirements", now, body="have a look")
    await board.set_capability_endpoint(
        node_id, "http://127.0.0.1:8899", 1_700_000_000)
    await board.set_edge_mind(
        mind_name, "operator", "operator", "systemd", 1_700_000_000)

    item = await board.work_item(item_id)
    assert item is not None
    assert item["title"] == "an epic"
    assert item["position"] == 7
    assert item["requirements"] == "one"
    assert item["qa_tests"] == "three"
    # The parent's kind rides along, and is None for a row with no parent
    # rather than absent: the console decides which key a story's parent is
    # reported under from this value.
    assert item["parent_kind"] is None

    session = await board.design_session(design_id)
    assert session is not None
    assert session["title"] == "a design"
    assert session["mind_id"] == a_mind
    assert session["repos"] == '["hive"]'
    # The five page columns take the schema's own defaults, so a session is
    # created with five empty objects without this layer restating them.
    assert session["requirements"] == "{}"
    assert session["accepted"] == "{}"
    assert session["revision"] == 1

    notice_id = queued["row"]["id"]
    assert await inspector.fetchval(
        'SELECT body FROM board."design_notices" WHERE id = $1',
        notice_id) == "have a look"

    endpoint = await board.capability_endpoint(node_id)
    assert endpoint is not None
    assert endpoint["endpoint"] == "http://127.0.0.1:8899"

    edge = await board.edge_mind(mind_name)
    assert edge is not None
    assert edge["profile"] == "operator"
    assert edge["deployment"] == "systemd"

    # Read back outside the package as well, because a layer that writes and
    # reads the same wrong column passes its own round trip.
    assert await inspector.fetchval(
        'SELECT title FROM board."work_items" WHERE id = $1',
        item_id) == "an epic"
    assert await inspector.fetchval(
        'SELECT deployment FROM board."edge_mind_settings" WHERE name = $1',
        mind_name) == "systemd"


async def test_an_identity_column_is_not_writable_through_an_insert(connected):
    """`design_notices.id` is `GENERATED BY DEFAULT`, which Postgres accepts a
    value for without advancing the sequence - so the row lands and the next
    insert collides on a key that is already there."""
    from hive_data import _sql
    from hive_data.errors import IdentityNotWritable

    with pytest.raises(IdentityNotWritable):
        _sql.insert(board.NOTICES, {"id": 1, "design_id": "x", "kind": "k",
                                    "page": "requirements", "created_at": 0.0})


# ------------------------------------------------------------ order and numbers

async def test_siblings_come_back_in_the_order_their_positions_give(
        connected):
    """Requirement two. The order is the one the positions state, and it is
    the same order twice - `ORDER BY position` alone leaves ties to the
    planner, and a board that reorders itself between two loads renumbers
    every story on the page."""
    ids = await _board()

    under_epic = await board.children(ids["epic"])
    assert [row["id"] for row in under_epic] == [
        ids["feature_one"], ids["feature_two"], ids["loose"]]
    assert [row["id"] for row in await board.children(
        ids["epic"], kind="feature")] == [
            ids["feature_one"], ids["feature_two"]]
    assert [row["id"] for row in await board.children(
        ids["feature_two"])] == [ids["story_three"], ids["story_four"]]

    # Reversed, and the listing follows. The assertion is on the order the
    # layer gives back, not on the positions it wrote.
    await board.renumber([ids["feature_two"], ids["feature_one"]])
    assert [row["id"] for row in await board.children(
        ids["epic"], kind="feature")] == [
            ids["feature_two"], ids["feature_one"]]


async def test_the_backlog_numbers_follow_the_tree_and_the_sibling_order(
        connected):
    """Requirement two's load-bearing half: every story's number appears in
    conversations and commit messages, so a port that shifts one is a port
    that renames work."""
    ids = await _board()
    numbers = await board.backlog_numbers()

    epic = numbers[ids["epic"]]
    assert numbers[ids["feature_one"]] == f"{epic}.1"
    assert numbers[ids["feature_two"]] == f"{epic}.2"
    assert numbers[ids["loose"]] == f"{epic}.3"
    assert numbers[ids["story_one"]] == f"{epic}.1.1"
    assert numbers[ids["story_two"]] == f"{epic}.1.2"
    assert numbers[ids["story_three"]] == f"{epic}.2.1"
    assert numbers[ids["story_four"]] == f"{epic}.2.2"

    # Moving a story inside its feature moves its number and its sibling's,
    # and touches nothing under the other feature.
    await board.renumber([ids["story_two"], ids["story_one"]])
    moved = await board.backlog_numbers()
    assert moved[ids["story_two"]] == f"{epic}.1.1"
    assert moved[ids["story_one"]] == f"{epic}.1.2"
    assert moved[ids["story_three"]] == f"{epic}.2.1"


async def test_an_epic_numbers_as_a_root_even_when_it_holds_a_parent(
        connected):
    """Carried over from the console's own numbering deliberately. The record
    the board renders carries no parent key for an epic, so an epic given a
    parent by a change of type numbers at the top level - and a port that
    corrected that would renumber the live board on the day it shipped."""
    ids = await _board()
    adopted = _id("e")
    await board.create_work_item(adopted, "epic", "an adopted epic", 50)
    await board.place(adopted, ids["epic"], 4)

    rows = await board.work_items()
    placed = next(row for row in rows if row["id"] == adopted)
    assert placed["parent_id"] == ids["epic"]
    assert board.numbering_parent(placed) is None

    numbers = board.numbers_for(rows)
    assert "." not in numbers[adopted]


async def test_a_row_whose_parent_is_not_in_the_rows_numbers_as_nothing():
    """A half-loaded board renders. An empty string rather than a missing
    key, because the number is put on the page and a KeyError there is a page
    that does not draw at all."""
    orphan = {"id": "s1", "kind": "story", "parent_id": "f-gone",
              "parent_kind": "feature"}
    assert board.numbers_for([orphan]) == {"s1": ""}


# ----------------------------------------------------------- the write lock

async def test_a_second_reorder_waits_for_the_first_to_finish(connected):
    """Requirement three. Every reorder reads the sibling list, decides a new
    numbering against it and writes the whole list back, so two of them
    interleaved is two siblings left holding one position with both callers
    told they succeeded."""
    ids = await _board()
    forward = [ids["story_one"], ids["story_two"]]
    backward = [ids["story_two"], ids["story_one"]]

    started = asyncio.Event()

    async def second() -> None:
        async with hive_data.transaction() as unit:
            await board.hold_board(unit)
            started.set()
            await board.renumber(backward, unit=unit)

    async with hive_data.transaction() as first:
        await board.hold_board(first)
        waiting = asyncio.create_task(second())
        # The second unit is inside `hold_board` and will not come out of it
        # while this one holds the key. Asserted rather than assumed: without
        # the hold it sails past and the interleave is a coin toss that
        # passes most runs.
        await asyncio.sleep(0.25)
        assert not started.is_set()
        assert not waiting.done()
        await board.renumber(forward, unit=first)

    await asyncio.wait_for(waiting, timeout=10)

    positions = [row["position"] for row in await board.children(
        ids["feature_one"])]
    assert len(set(positions)) == len(positions)
    assert [row["id"] for row in await board.children(
        ids["feature_one"])] == backward


async def test_the_hold_has_to_be_the_first_thing_in_its_unit(connected):
    """A hold taken after a read does not protect that read: two units both
    read the sibling list, then both take the key in turn, and both write the
    numbering they computed against a list that has since changed."""
    ids = await _board()
    async with hive_data.transaction() as unit:
        await board.children(ids["epic"], unit=unit)
        with pytest.raises(TransactionMisuse):
            await board.hold_board(unit)


async def test_a_renumber_naming_one_row_twice_is_refused(connected):
    """Two positions for one row is a sibling list that cannot be written,
    and the one that would land is whichever the planner reached last."""
    ids = await _board()
    with pytest.raises(ValueRefused):
        await board.renumber([ids["story_one"], ids["story_one"]])
    assert [row["id"] for row in await board.children(
        ids["feature_one"])] == [ids["story_one"], ids["story_two"]]


# ------------------------------------------------------- the deferred key

async def test_a_child_written_before_its_parent_lands_in_one_unit(
        connected, inspector):
    """Requirement four. The key is `DEFERRABLE INITIALLY DEFERRED`, so a
    subtree can be written in whatever order the caller has it in and the
    constraint is checked once, at commit - rather than the caller having to
    sort its own rows topologically or the layer having to turn the key off."""
    epic = _id("e")
    story = _id("s")
    async with hive_data.transaction() as unit:
        await board.create_work_item(
            story, "story", "the child", 2, parent_id=epic, status="backlog",
            unit=unit)
        await board.create_work_item(epic, "epic", "the parent", 1, unit=unit)

    assert await inspector.fetchval(
        'SELECT parent_id FROM board."work_items" WHERE id = $1',
        story) == epic


async def test_a_parent_that_never_arrives_refuses_the_whole_unit(
        connected, inspector):
    """The cost of the deferred key, stated rather than hidden: a `parent_id`
    naming nothing is refused at the commit, so the error comes out of the
    `async with` and not out of the operation - and nothing lands."""
    story = _id("s")
    with pytest.raises(MissingReference):
        async with hive_data.transaction() as unit:
            await board.create_work_item(
                story, "story", "an orphan", 1, parent_id=_id("f"),
                status="backlog", unit=unit)

    assert await inspector.fetchval(
        'SELECT count(*) FROM board."work_items" WHERE id = $1', story) == 0


async def test_deleting_a_row_takes_everything_beneath_it(
        connected, inspector):
    """Three levels, walked rather than assumed. A cascade that hard-codes
    the depth leaves rows behind the moment the shape changes - and those are
    exactly the rows that cannot be deleted afterwards, because the only
    delete button is on the tree they have fallen out of."""
    ids = await _board()
    deeper = _id("s")
    await board.create_work_item(
        deeper, "story", "a story under a story", 1,
        parent_id=ids["story_one"], status="backlog")

    assert {row["id"] for row in await board.descendants(ids["epic"])} == {
        ids["feature_one"], ids["feature_two"], ids["loose"],
        ids["story_one"], ids["story_two"], ids["story_three"],
        ids["story_four"], deeper}

    gone = await board.delete_subtree(ids["epic"])
    assert gone["rows"] == 8 + 1

    assert await inspector.fetchval(
        'SELECT count(*) FROM board."work_items" WHERE id = ANY($1)',
        list(ids.values()) + [deeper]) == 0


async def test_an_item_cannot_be_made_its_own_parent(connected):
    """A row pointing at itself renders nowhere and makes the recursive read
    that finds a subtree never end."""
    ids = await _board()
    with pytest.raises(ValueRefused):
        await board.place(ids["feature_one"], ids["feature_one"], 1)


# --------------------------------------------------------- design sessions

async def test_a_page_written_against_a_stale_revision_changes_nothing(
        connected, inspector, a_notice_time):
    """Two tabs on one design session. The revision travels into the UPDATE,
    so the second write reports zero rows rather than silently winning."""
    now = a_notice_time
    design_id = _id("d")
    await board.create_design_session(design_id, "a design", now, now)

    first = await board.write_page(
        design_id, "requirements", '{"a": 1}', "{}", "{}", 1, now)
    assert first["rows"] == 1
    assert await inspector.fetchval(
        'SELECT revision FROM board."design_sessions" WHERE id = $1',
        design_id) == 2

    stale = await board.write_page(
        design_id, "requirements", '{"b": 2}', "{}", "{}", 1, now + 1)
    assert stale["rows"] == 0
    assert await inspector.fetchval(
        'SELECT requirements FROM board."design_sessions" WHERE id = $1',
        design_id) == '{"a": 1}'


async def test_a_page_name_that_is_not_a_page_is_refused(
        connected, a_notice_time):
    """The page names a column, and a column name cannot travel as a
    parameter - so it is checked against the declaration rather than quoted
    and hoped for."""
    design_id = _id("d")
    await board.create_design_session(
        design_id, "a design", a_notice_time, a_notice_time)
    with pytest.raises(ValueRefused):
        await board.write_page(
            design_id, 'title" = $1, "requirements', "{}", "{}", "{}", 1,
            a_notice_time)


async def test_attaching_keeps_the_surface_it_was_not_told_about(
        connected, inspector, a_notice_time):
    """A mind re-claims a session on every turn that touches it and knows the
    conversation; the surface was decided when the page was opened."""
    now = a_notice_time
    design_id = _id("d")
    await board.create_design_session(
        design_id, "a design", now, now, surface="console")

    await board.attach_design_session(design_id, None, now + 1)
    row = await board.design_session(design_id)
    assert row is not None
    assert row["surface"] == "console"
    # And the revision is deliberately left where it was, or a mind
    # re-claiming would invalidate whatever is half-typed on the page.
    assert row["revision"] == 1


async def test_deleting_a_design_session_takes_its_notices(
        connected, inspector, a_notice_time):
    """`design_notices.design_id` carries no foreign key, so nothing in the
    database removes them. Left behind they are undelivered rows the delivery
    path keeps picking up and joining to a session that is gone - never
    delivered, never cleared."""
    now = a_notice_time
    design_id = _id("d")
    await board.create_design_session(design_id, "a design", now, now)
    await board.queue_notice(design_id, "submitted", "tests", now)

    gone = await board.delete_design_session(design_id)
    assert gone["rows"] == 1
    assert await inspector.fetchval(
        'SELECT count(*) FROM board."design_notices" WHERE design_id = $1',
        design_id) == 0


# ------------------------------------------------------------------ notices

async def test_a_notice_is_claimed_once(connected, inspector, a_notice_time):
    """Delivered twice is two harnesses starting work in one working tree,
    each unaware of the other, so the loser is told it lost rather than
    handed a second copy."""
    now = a_notice_time
    design_id = _id("d")
    await board.create_design_session(design_id, "a design", now, now)
    queued = await board.queue_notice(design_id, "build", "functions", now)
    notice_id = queued["row"]["id"]

    won = await board.claim_notice(notice_id, now + 1)
    assert won["rows"] == 1
    assert won["row"]["page"] == "functions"

    lost = await board.claim_notice(notice_id, now + 2)
    assert lost["rows"] == 0
    assert await inspector.fetchval(
        'SELECT claimed_at FROM board."design_notices" WHERE id = $1',
        notice_id) == now + 1


async def test_a_released_notice_can_be_claimed_again(
        connected, a_notice_time):
    """A transport that raised has delivered nothing, and a notice left
    claimed is a submission nobody hears about again."""
    now = a_notice_time
    design_id = _id("d")
    await board.create_design_session(design_id, "a design", now, now)
    queued = await board.queue_notice(design_id, "build", "tests", now)
    notice_id = queued["row"]["id"]

    await board.claim_notice(notice_id, now + 1)
    released = await board.release_notice(notice_id)
    assert released["rows"] == 1
    assert (await board.claim_notice(notice_id, now + 3))["rows"] == 1


async def test_a_delivered_notice_is_not_delivered_twice(
        connected, inspector, a_notice_time):
    """Delivery is acknowledged after the fact, and a second acknowledgement
    for one send is a second send nobody made - which also must not be able
    to un-deliver one that landed."""
    now = a_notice_time
    design_id = _id("d")
    await board.create_design_session(design_id, "a design", now, now)
    queued = await board.queue_notice(design_id, "build", "tests", now)
    notice_id = queued["row"]["id"]

    assert (await board.mark_delivered(notice_id, now + 1))["rows"] == 1
    assert (await board.mark_delivered(notice_id, now + 2))["rows"] == 0
    assert (await board.release_notice(notice_id))["rows"] == 0
    assert await inspector.fetchval(
        'SELECT delivered_at FROM board."design_notices" WHERE id = $1',
        notice_id) == now + 1


async def test_pending_notices_carry_the_conversation_to_reach(
        connected, a_notice_time, a_mind):
    """The delivery path needs the conversation, the surface and the mind to
    send anything at all, and a second read for them is a notice whose
    session was deleted in between."""
    now = a_notice_time
    design_id = _id("d")
    await board.create_design_session(
        design_id, "a design", now, now, surface="telegram", mind_id=a_mind)
    await board.queue_notice(design_id, "submitted", "tests", now + 2)
    await board.queue_notice(design_id, "submitted", "requirements", now + 1)

    pending = await board.pending_notices(design_id)
    assert [row["page"] for row in pending] == ["requirements", "tests"]
    assert pending[0]["surface"] == "telegram"
    assert pending[0]["mind_id"] == a_mind
    assert pending[0]["title"] == "a design"

    await board.mark_delivered(pending[0]["id"], now + 5)
    assert [row["page"] for row in await board.pending_notices(design_id)] == [
        "tests"]


# --------------------------------------------------------------- settings

async def test_a_cleared_endpoint_is_gone_rather_than_blank(
        connected, inspector):
    """The console falls back to a default when a node has no endpoint, so a
    row holding an empty string is a node pointed at nothing at all."""
    node_id = _id("node-")
    await board.set_capability_endpoint(
        node_id, "http://127.0.0.1:8899", 1_700_000_000)
    await board.set_capability_endpoint(
        node_id, "http://127.0.0.1:11434", 1_700_000_100)
    endpoint = await board.capability_endpoint(node_id)
    assert endpoint is not None
    assert endpoint["endpoint"] == "http://127.0.0.1:11434"

    cleared = await board.clear_capability_endpoint(node_id)
    assert cleared["rows"] == 1
    assert await board.capability_endpoint(node_id) is None
    assert await inspector.fetchval(
        'SELECT count(*) FROM board."capability_node_settings" '
        "WHERE node_id = $1", node_id) == 0
    # Clearing one that is not there reports nothing changed, rather than
    # telling the page a delete landed somewhere.
    assert (await board.clear_capability_endpoint(node_id))["rows"] == 0


async def test_work_in_one_transaction_is_undone_together(
        connected, inspector):
    """The test that fails if an operation ignores the unit it was handed and
    opens a connection of its own: nothing here is committed."""
    item_id = _id("e")
    node_id = _id("node-")

    with pytest.raises(Deliberate):
        async with hive_data.transaction() as unit:
            await board.create_work_item(
                item_id, "epic", "an epic", 1, unit=unit)
            await board.set_capability_endpoint(
                node_id, "http://127.0.0.1:8899", 1_700_000_000, unit=unit)
            raise Deliberate

    assert await inspector.fetchval(
        'SELECT count(*) FROM board."work_items" WHERE id = $1', item_id) == 0
    assert await inspector.fetchval(
        'SELECT count(*) FROM board."capability_node_settings" '
        "WHERE node_id = $1", node_id) == 0
