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


def test_existing_board_database_without_metadata_columns_is_upgraded(monkeypatch, tmp_path):
    legacy_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(legacy_path)
    connection.execute("CREATE TABLE work_items (id TEXT PRIMARY KEY, kind TEXT NOT NULL, parent_id TEXT REFERENCES work_items(id), title TEXT NOT NULL, status TEXT, position INTEGER NOT NULL)")
    connection.execute("INSERT INTO work_items VALUES ('e-old', 'epic', NULL, 'Existing epic', NULL, 1)")
    connection.commit()
    connection.close()
    monkeypatch.setenv("HIVE_DB_PATH", str(legacy_path))
    importlib.reload(store)

    assert store.get_data()["epics"] == [{"id": "e-old", "title": "Existing epic", "description": "", "requirements": "", "acceptance_criteria": "", "qa_tests": ""}]


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
            (store._MIN_POSITION + 99, first["id"]),
        )
    assert [e["id"] for e in store.list_epics()] == [second["id"], first["id"]]


def test_an_epic_created_after_several_deletions_sorts_first():
    made = [store.create_epic(f"Epic {n}") for n in range(5)]
    store.delete_epic(made[1]["id"])
    store.delete_epic(made[2]["id"])
    latest = store.create_epic("Newest")
    assert [e["id"] for e in store.list_epics()][0] == latest["id"]


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


def test_a_title_is_stored_without_its_surrounding_whitespace():
    epic = store.create_epic("  Padded  ")
    assert epic["title"] == "Padded"
    assert store.list_epics()[0]["title"] == "Padded"
    assert store.update_epic(epic["id"], title="\t Renamed \n")["title"] == "Renamed"


def test_a_title_past_the_ceiling_is_refused():
    assert store.create_epic("a" * store.MAX_TITLE_LENGTH)["title"]
    with pytest.raises(ValueError, match="at most"):
        store.create_epic("a" * (store.MAX_TITLE_LENGTH + 1))
    epic = store.create_epic("Fine")
    with pytest.raises(ValueError, match="at most"):
        store.update_epic(epic["id"], title="a" * (store.MAX_TITLE_LENGTH + 1))


def test_a_feature_moved_to_another_epic_is_stored_under_the_new_one():
    origin = store.create_epic("Origin")
    destination = store.create_epic("Destination")
    feature = store.create_feature(origin["id"], "Feature")
    story = store.create_story("Story", feature_id=feature["id"])

    store.update_feature(feature["id"], epic_id=destination["id"])

    assert store.list_features()[0]["epic_id"] == destination["id"]
    # The story rides along: it hangs off the feature, not off the epic.
    assert store.list_stories()[0]["feature_id"] == feature["id"]
    assert store.count_descendants(destination["id"]) == {"features": 1, "stories": 1}
    assert store.count_descendants(origin["id"]) == {"features": 0, "stories": 0}


def test_a_story_moved_to_another_feature_is_stored_under_the_new_one():
    epic = store.create_epic("Epic")
    origin = store.create_feature(epic["id"], "Origin")
    destination = store.create_feature(epic["id"], "Destination")
    story = store.create_story("Story", feature_id=origin["id"])

    store.update_story(story["id"], feature_id=destination["id"])

    assert store.list_stories()[0]["feature_id"] == destination["id"]
    assert store.list_stories()[0]["id"] == story["id"]
    assert store.get_data()["stories"] == store.list_stories()


def test_every_status_the_board_offers_is_accepted_and_nothing_else_is():
    epic = store.create_epic("Epic")
    story = store.create_story("Story", epic_id=epic["id"])
    for status in store.STATUSES:
        assert store.update_story(story["id"], status=status)["status"] == status
        assert store.list_stories()[0]["status"] == status
    with pytest.raises(ValueError, match="unsupported status"):
        store.update_story(story["id"], status="archived")
    assert store.list_stories()[0]["status"] == store.STATUSES[-1]


def test_each_kind_is_identifiable_from_its_id():
    epic = store.create_epic("Epic")
    feature = store.create_feature(epic["id"], "Feature")
    story = store.create_story("Story", feature_id=feature["id"])
    assert (epic["id"][0], feature["id"][0], story["id"][0]) == ("e", "f", "s")
