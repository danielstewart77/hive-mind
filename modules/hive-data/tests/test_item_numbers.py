"""Every work item has a permanent whole-number ID (step 14, story 3.7.7).

One count shared by every kind, never reused, never changed by a move. The
scratch database here is made once per run and never emptied, so every
assertion is relative to the number the previous write was given rather
than an absolute one - an absolute "the first item is 1" would depend on
which tests ran first.

Read back with a plain driver connection, for the reason `test_board.py`
gives.
"""

from __future__ import annotations

import uuid

from hive_data import board


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


async def _number(inspector, item_id: str) -> int:
    return await inspector.fetchval(
        "SELECT item_number FROM board.work_items WHERE id = $1", item_id)


async def test_each_new_item_of_any_kind_takes_the_next_number_of_one_count(
        connected, inspector):
    epic, feature, story = _id("e"), _id("f"), _id("s")
    await board.create_work_item(epic, "epic", "an epic", 1)
    await board.create_work_item(feature, "feature", "a feature", 1,
                                 parent_id=epic)
    await board.create_work_item(story, "story", "a story", 1,
                                 parent_id=feature, status="backlog")

    first = await _number(inspector, epic)
    assert isinstance(first, int)
    assert [await _number(inspector, feature),
            await _number(inspector, story)] == [first + 1, first + 2]
    # The layer's own read carries it, which is what the console shows.
    assert (await board.work_item(story))["item_number"] == first + 2


async def test_a_deleted_items_number_is_never_handed_out_again(
        connected, inspector):
    gone = _id("e")
    await board.create_work_item(gone, "epic", "deleted next", 1)
    deleted = await _number(inspector, gone)
    await board.delete_subtree(gone)

    after = _id("e")
    await board.create_work_item(after, "epic", "created after", 1)
    assert await _number(inspector, after) == deleted + 1


async def test_an_items_number_survives_every_move_and_change_of_type(
        connected, inspector):
    epic, other, story = _id("e"), _id("e"), _id("s")
    await board.create_work_item(epic, "epic", "home", 1)
    await board.create_work_item(other, "epic", "elsewhere", 2)
    await board.create_work_item(story, "story", "moves about", 1,
                                 parent_id=epic, status="backlog")
    number = await _number(inspector, story)

    await board.place(story, other, 5)
    assert await _number(inspector, story) == number
    await board.renumber([story], start=1)
    assert await _number(inspector, story) == number
    await board.retype(story, "feature", status="backlog", parent_id=epic,
                       position=9)
    assert await _number(inspector, story) == number
    await board.update_work_item(story, {"title": "renamed"})
    assert await _number(inspector, story) == number
