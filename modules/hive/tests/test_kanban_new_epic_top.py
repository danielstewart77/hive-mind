"""A new epic added from the backlog goes to the top of the list."""
import kanban_data as store


def test_a_new_epic_is_listed_above_the_existing_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    first = store.create_epic("First")
    second = store.create_epic("Second")
    added = store.create_epic("Added")
    ids = [e["id"] for e in store.list_epics()]
    assert ids == [added["id"], second["id"], first["id"]]
