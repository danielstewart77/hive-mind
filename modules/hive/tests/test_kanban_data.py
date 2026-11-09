"""The kanban store, against the suite's scratch database."""
import os

import asyncpg
import pytest

import kanban_data as store
import store as data_store
from hive_site.testing import refuse_unless_scratch


async def _execute(sql, *args):
    """A write made behind the store's back, as another writer would."""
    connection = await asyncpg.connect(
        refuse_unless_scratch(os.environ["HIVE_DATABASE_URL"]))
    try:
        await connection.execute(sql, *args)
    finally:
        await connection.close()


async def test_a_new_board_holds_nothing_and_each_list_holds_only_its_own_kind():
    assert await store.get_data() == {"epics": [], "features": [], "stories": []}
    epic = await store.create_epic("Only epic")
    feature = await store.create_feature(epic["id"], "A feature")
    await store.create_story("A story", feature_id=feature["id"])
    assert [e["id"] for e in await store.list_epics()] == [epic["id"]]
    assert [f["id"] for f in await store.list_features()] == [feature["id"]]
    assert len(await store.list_stories()) == 1


async def test_written_work_is_read_back_by_a_process_that_never_saw_the_write():
    """The board is in the database, not in this process.

    Proved by closing the store and opening it again, which
    is as near as a test gets to the next request arriving at a worker that
    has never seen the write.
    """
    epic = await store.create_epic("Survives")
    story = await store.create_story("Also survives", epic_id=epic["id"])
    await data_store.close()
    await data_store.open()
    data = await store.get_data()
    assert [e["title"] for e in data["epics"]] == ["Survives"]
    assert [s["title"] for s in data["stories"]] == ["Also survives"]
    assert data["stories"][0]["id"] == story["id"]


async def test_the_stored_position_decides_the_order_not_the_insertion_order():
    first = await store.create_epic("First")
    second = await store.create_epic("Second")
    await _execute("UPDATE board.work_items SET position = $1 WHERE id = $2",
                   data_store.MIN_POSITION + 99, first["id"])
    assert [e["id"] for e in await store.list_epics()] == [second["id"], first["id"]]


async def test_an_epic_created_after_several_deletions_sorts_first():
    made = [await store.create_epic(f"Epic {n}") for n in range(5)]
    await store.delete_epic(made[1]["id"])
    await store.delete_epic(made[2]["id"])
    latest = await store.create_epic("Newest")
    assert [e["id"] for e in await store.list_epics()][0] == latest["id"]


async def test_deleting_an_epic_takes_its_features_and_every_story_beneath_them():
    epic = await store.create_epic("Doomed")
    survivor = await store.create_epic("Survivor")
    feature = await store.create_feature(epic["id"], "Doomed feature")
    nested = await store.create_story("Under the feature", feature_id=feature["id"])
    direct = await store.create_story("Under the epic", epic_id=epic["id"])
    safe = await store.create_story("Elsewhere", epic_id=survivor["id"])

    await store.delete_epic(epic["id"])

    data = await store.get_data()
    assert [e["id"] for e in data["epics"]] == [survivor["id"]]
    assert data["features"] == []
    assert [s["id"] for s in data["stories"]] == [safe["id"]]


async def test_deleting_a_feature_takes_its_stories_and_leaves_the_epic_alone():
    epic = await store.create_epic("Epic")
    feature = await store.create_feature(epic["id"], "Doomed feature")
    other = await store.create_feature(epic["id"], "Other feature")
    await store.create_story("Goes", feature_id=feature["id"])
    kept = await store.create_story("Stays", feature_id=other["id"])

    await store.delete_feature(feature["id"])

    data = await store.get_data()
    assert [e["id"] for e in data["epics"]] == [epic["id"]]
    assert [f["id"] for f in data["features"]] == [other["id"]]
    assert [s["id"] for s in data["stories"]] == [kept["id"]]


async def test_deleting_a_story_takes_only_that_story():
    epic = await store.create_epic("Epic")
    feature = await store.create_feature(epic["id"], "Feature")
    doomed = await store.create_story("Goes", feature_id=feature["id"])
    kept = await store.create_story("Stays", feature_id=feature["id"])

    await store.delete_story(doomed["id"])

    data = await store.get_data()
    assert [f["id"] for f in data["features"]] == [feature["id"]]
    assert [s["id"] for s in data["stories"]] == [kept["id"]]


async def test_a_new_story_starts_in_the_backlog():
    epic = await store.create_epic("Epic")
    assert (await store.create_story("Fresh", epic_id=epic["id"]))["status"] == "backlog"


async def test_a_story_reports_the_parent_it_actually_has_and_not_the_other_kind():
    epic = await store.create_epic("Epic")
    feature = await store.create_feature(epic["id"], "Feature")
    under_epic = await store.create_story("Direct", epic_id=epic["id"])
    under_feature = await store.create_story("Nested", feature_id=feature["id"])

    by_id = {s["id"]: s for s in await store.list_stories()}
    assert by_id[under_epic["id"]]["epic_id"] == epic["id"]
    assert "feature_id" not in by_id[under_epic["id"]]
    assert by_id[under_feature["id"]]["feature_id"] == feature["id"]
    assert "epic_id" not in by_id[under_feature["id"]]
    assert "parent_id" not in by_id[under_epic["id"]]
    assert (await store.list_features())[0]["epic_id"] == epic["id"]


async def test_a_refused_write_leaves_the_board_as_it_was():
    epic = await store.create_epic("Epic")
    before = await store.get_data()

    with pytest.raises(ValueError, match="title must be a nonblank string"):
        await store.create_epic("   ")
    with pytest.raises(ValueError, match="exactly one"):
        await store.create_story("No parent")
    with pytest.raises(ValueError, match="exactly one"):
        await store.create_story("Two parents", feature_id="f", epic_id=epic["id"])
    with pytest.raises(KeyError, match="Epic not found"):
        await store.create_feature("missing", "Feature")
    with pytest.raises(ValueError, match="unsupported status"):
        await store.create_story("Bad status", epic_id=epic["id"], status="nope")
    with pytest.raises(ValueError, match="unsupported fields"):
        await store.update_epic(epic["id"], unknown="x")
    with pytest.raises(ValueError, match="title must be a nonblank string"):
        await store.update_epic(epic["id"], title="  ")

    assert await store.get_data() == before


async def test_a_parent_of_the_wrong_kind_is_refused():
    epic = await store.create_epic("Epic")
    feature = await store.create_feature(epic["id"], "Feature")
    story = await store.create_story("Story", feature_id=feature["id"])

    with pytest.raises(KeyError, match="Epic not found"):
        await store.create_feature(story["id"], "Feature under a story")
    with pytest.raises(KeyError, match="Feature not found"):
        await store.create_story("Story under an epic by the wrong key", feature_id=epic["id"])
    with pytest.raises(KeyError, match="Epic not found"):
        await store.update_feature(feature["id"], epic_id=feature["id"])


async def test_a_story_cannot_be_left_with_no_parent_or_moved_onto_two():
    epic = await store.create_epic("Epic")
    feature = await store.create_feature(epic["id"], "Feature")
    story = await store.create_story("Story", feature_id=feature["id"])

    with pytest.raises(ValueError, match="exactly one"):
        await store.update_story(story["id"], feature_id=None)
    with pytest.raises(ValueError, match="exactly one"):
        await store.update_story(story["id"], feature_id=feature["id"], epic_id=epic["id"])

    moved = await store.update_story(story["id"], epic_id=epic["id"])
    assert moved["epic_id"] == epic["id"] and "feature_id" not in moved


async def test_acting_on_a_row_another_writer_already_deleted_is_refused():
    epic = await store.create_epic("Epic")
    await _execute("DELETE FROM board.work_items WHERE id = $1", epic["id"])

    with pytest.raises(KeyError, match="Epic not found"):
        await store.update_epic(epic["id"], title="New title")
    with pytest.raises(KeyError, match="Epic not found"):
        await store.delete_epic(epic["id"])


async def test_descendants_counts_what_a_delete_would_take():
    epic = await store.create_epic("Epic")
    feature = await store.create_feature(epic["id"], "Feature")
    await store.create_story("Nested", feature_id=feature["id"])
    await store.create_story("Direct", epic_id=epic["id"])

    assert await store.count_descendants(epic["id"]) == {"features": 1, "stories": 2}
    assert await store.count_descendants(feature["id"]) == {"features": 0, "stories": 1}


async def test_ids_are_unique_across_many_creations():
    ids = {(await store.create_epic(f"Epic {n}"))["id"] for n in range(50)}
    assert len(ids) == 50


async def test_a_title_is_stored_without_its_surrounding_whitespace():
    epic = await store.create_epic("  Padded  ")
    assert epic["title"] == "Padded"
    assert (await store.list_epics())[0]["title"] == "Padded"
    assert (await store.update_epic(epic["id"], title="\t Renamed \n"))["title"] == "Renamed"


async def test_a_title_past_the_ceiling_is_refused():
    assert (await store.create_epic("a" * store.MAX_TITLE_LENGTH))["title"]
    with pytest.raises(ValueError, match="at most"):
        await store.create_epic("a" * (store.MAX_TITLE_LENGTH + 1))
    epic = await store.create_epic("Fine")
    with pytest.raises(ValueError, match="at most"):
        await store.update_epic(epic["id"], title="a" * (store.MAX_TITLE_LENGTH + 1))


async def test_a_feature_moved_to_another_epic_is_stored_under_the_new_one():
    origin = await store.create_epic("Origin")
    destination = await store.create_epic("Destination")
    feature = await store.create_feature(origin["id"], "Feature")
    story = await store.create_story("Story", feature_id=feature["id"])

    await store.update_feature(feature["id"], epic_id=destination["id"])

    assert (await store.list_features())[0]["epic_id"] == destination["id"]
    # The story rides along: it hangs off the feature, not off the epic.
    assert (await store.list_stories())[0]["feature_id"] == feature["id"]
    assert await store.count_descendants(destination["id"]) == {"features": 1, "stories": 1}
    assert await store.count_descendants(origin["id"]) == {"features": 0, "stories": 0}


async def test_a_story_moved_to_another_feature_is_stored_under_the_new_one():
    epic = await store.create_epic("Epic")
    origin = await store.create_feature(epic["id"], "Origin")
    destination = await store.create_feature(epic["id"], "Destination")
    story = await store.create_story("Story", feature_id=origin["id"])

    await store.update_story(story["id"], feature_id=destination["id"])

    assert (await store.list_stories())[0]["feature_id"] == destination["id"]
    assert (await store.list_stories())[0]["id"] == story["id"]
    assert (await store.get_data())["stories"] == await store.list_stories()


async def test_every_status_the_board_offers_is_accepted_and_nothing_else_is():
    epic = await store.create_epic("Epic")
    story = await store.create_story("Story", epic_id=epic["id"])
    for status in store.STATUSES:
        assert (await store.update_story(story["id"], status=status))["status"] == status
        assert (await store.list_stories())[0]["status"] == status
    with pytest.raises(ValueError, match="unsupported status"):
        await store.update_story(story["id"], status="archived")
    assert (await store.list_stories())[0]["status"] == store.STATUSES[-1]


async def test_each_kind_is_identifiable_from_its_id():
    epic = await store.create_epic("Epic")
    feature = await store.create_feature(epic["id"], "Feature")
    story = await store.create_story("Story", feature_id=feature["id"])
    assert (epic["id"][0], feature["id"][0], story["id"][0]) == ("e", "f", "s")
