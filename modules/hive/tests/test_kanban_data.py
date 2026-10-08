"""The kanban store, against its own SQLite file."""
import importlib
import os
import sqlite3

import pytest

import kanban_data as store


@pytest.fixture(autouse=True)
def fresh_store():
    """Each test gets the module bound to this test's HIVE_DB_PATH."""
    importlib.reload(store)
    yield


def _db():
    return sqlite3.connect(os.environ["HIVE_DB_PATH"])


def test_a_new_board_holds_nothing_and_each_list_holds_only_its_own_kind():
    assert store.get_data() == {"epics": [], "features": [], "stories": []}
    epic = store.create_epic("Only epic")
    feature = store.create_feature(epic["id"], "A feature")
    store.create_story("A story", feature_id=feature["id"])
    assert [e["id"] for e in store.list_epics()] == [epic["id"]]
    assert [f["id"] for f in store.list_features()] == [feature["id"]]
    assert len(store.list_stories()) == 1


def test_written_work_is_read_back_by_a_process_that_never_saw_the_write():
    epic = store.create_epic("Survives")
    story = store.create_story("Also survives", epic_id=epic["id"])
    importlib.reload(store)
    data = store.get_data()
    assert [e["title"] for e in data["epics"]] == ["Survives"]
    assert [s["title"] for s in data["stories"]] == ["Also survives"]
    assert data["stories"][0]["id"] == story["id"]


def test_the_stored_position_decides_the_order_not_the_insertion_order():
    first = store.create_epic("First")
    second = store.create_epic("Second")
    with _db() as connection:
        connection.execute(
            "UPDATE work_items SET position = ? WHERE id = ?",
            (store._MIN_POSITION - 1, second["id"]),
        )
    assert [e["id"] for e in store.list_epics()] == [second["id"], first["id"]]


def test_an_item_created_after_several_deletions_sorts_last():
    made = [store.create_epic(f"Epic {n}") for n in range(5)]
    store.delete_epic(made[1]["id"])
    store.delete_epic(made[2]["id"])
    latest = store.create_epic("Newest")
    assert [e["id"] for e in store.list_epics()][-1] == latest["id"]


def test_deleting_an_epic_takes_its_features_and_every_story_beneath_them():
    epic = store.create_epic("Doomed")
    survivor = store.create_epic("Survivor")
    feature = store.create_feature(epic["id"], "Doomed feature")
    nested = store.create_story("Under the feature", feature_id=feature["id"])
    direct = store.create_story("Under the epic", epic_id=epic["id"])
    safe = store.create_story("Elsewhere", epic_id=survivor["id"])

    store.delete_epic(epic["id"])

    data = store.get_data()
    assert [e["id"] for e in data["epics"]] == [survivor["id"]]
    assert data["features"] == []
    assert [s["id"] for s in data["stories"]] == [safe["id"]]
    assert {nested["id"], direct["id"]} not in [{s["id"]} for s in data["stories"]]


def test_deleting_a_feature_takes_its_stories_and_leaves_the_epic_alone():
    epic = store.create_epic("Epic")
    feature = store.create_feature(epic["id"], "Doomed feature")
    other = store.create_feature(epic["id"], "Other feature")
    store.create_story("Goes", feature_id=feature["id"])
    kept = store.create_story("Stays", feature_id=other["id"])

    store.delete_feature(feature["id"])

    data = store.get_data()
    assert [e["id"] for e in data["epics"]] == [epic["id"]]
    assert [f["id"] for f in data["features"]] == [other["id"]]
    assert [s["id"] for s in data["stories"]] == [kept["id"]]


def test_deleting_a_story_takes_only_that_story():
    epic = store.create_epic("Epic")
    feature = store.create_feature(epic["id"], "Feature")
    doomed = store.create_story("Goes", feature_id=feature["id"])
    kept = store.create_story("Stays", feature_id=feature["id"])

    store.delete_story(doomed["id"])

    data = store.get_data()
    assert [f["id"] for f in data["features"]] == [feature["id"]]
    assert [s["id"] for s in data["stories"]] == [kept["id"]]


def test_a_new_story_starts_in_the_backlog():
    epic = store.create_epic("Epic")
    assert store.create_story("Fresh", epic_id=epic["id"])["status"] == "backlog"


def test_a_story_reports_the_parent_it_actually_has_and_not_the_other_kind():
    epic = store.create_epic("Epic")
    feature = store.create_feature(epic["id"], "Feature")
    under_epic = store.create_story("Direct", epic_id=epic["id"])
    under_feature = store.create_story("Nested", feature_id=feature["id"])

    by_id = {s["id"]: s for s in store.list_stories()}
    assert by_id[under_epic["id"]]["epic_id"] == epic["id"]
    assert "feature_id" not in by_id[under_epic["id"]]
    assert by_id[under_feature["id"]]["feature_id"] == feature["id"]
    assert "epic_id" not in by_id[under_feature["id"]]
    assert "parent_id" not in by_id[under_epic["id"]]
    assert store.list_features()[0]["epic_id"] == epic["id"]


def test_a_refused_write_leaves_the_board_as_it_was():
    epic = store.create_epic("Epic")
    before = store.get_data()

    with pytest.raises(ValueError, match="title must be a nonblank string"):
        store.create_epic("   ")
    with pytest.raises(ValueError, match="exactly one"):
        store.create_story("No parent")
    with pytest.raises(ValueError, match="exactly one"):
        store.create_story("Two parents", feature_id="f", epic_id=epic["id"])
    with pytest.raises(KeyError, match="Epic not found"):
        store.create_feature("missing", "Feature")
    with pytest.raises(ValueError, match="unsupported status"):
        store.create_story("Bad status", epic_id=epic["id"], status="nope")
    with pytest.raises(ValueError, match="unsupported fields"):
        store.update_epic(epic["id"], unknown="x")
    with pytest.raises(ValueError, match="title must be a nonblank string"):
        store.update_epic(epic["id"], title="  ")

    assert store.get_data() == before


def test_a_parent_of_the_wrong_kind_is_refused():
    epic = store.create_epic("Epic")
    feature = store.create_feature(epic["id"], "Feature")
    story = store.create_story("Story", feature_id=feature["id"])

    with pytest.raises(KeyError, match="Epic not found"):
        store.create_feature(story["id"], "Feature under a story")
    with pytest.raises(KeyError, match="Feature not found"):
        store.create_story("Story under an epic by the wrong key", feature_id=epic["id"])
    with pytest.raises(KeyError, match="Epic not found"):
        store.update_feature(feature["id"], epic_id=feature["id"])


def test_a_story_cannot_be_left_with_no_parent_or_moved_onto_two():
    epic = store.create_epic("Epic")
    feature = store.create_feature(epic["id"], "Feature")
    story = store.create_story("Story", feature_id=feature["id"])

    with pytest.raises(ValueError, match="exactly one"):
        store.update_story(story["id"], feature_id=None)
    with pytest.raises(ValueError, match="exactly one"):
        store.update_story(story["id"], feature_id=feature["id"], epic_id=epic["id"])

    moved = store.update_story(story["id"], epic_id=epic["id"])
    assert moved["epic_id"] == epic["id"] and "feature_id" not in moved


def test_acting_on_a_row_another_writer_already_deleted_is_refused():
    epic = store.create_epic("Epic")
    with _db() as connection:
        connection.execute("DELETE FROM work_items WHERE id = ?", (epic["id"],))

    with pytest.raises(KeyError, match="Epic not found"):
        store.update_epic(epic["id"], title="New title")
    with pytest.raises(KeyError, match="Epic not found"):
        store.delete_epic(epic["id"])


def test_descendants_counts_what_a_delete_would_take():
    epic = store.create_epic("Epic")
    feature = store.create_feature(epic["id"], "Feature")
    store.create_story("Nested", feature_id=feature["id"])
    store.create_story("Direct", epic_id=epic["id"])

    assert store.count_descendants(epic["id"]) == {"features": 1, "stories": 2}
    assert store.count_descendants(feature["id"]) == {"features": 0, "stories": 1}


def test_ids_are_unique_across_many_creations():
    ids = {store.create_epic(f"Epic {n}")["id"] for n in range(50)}
    assert len(ids) == 50
