"""A new epic added from the backlog goes to the top of the list."""
import kanban_data as store


async def test_a_new_epic_is_listed_above_the_existing_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    first = await store.create_epic("First")
    second = await store.create_epic("Second")
    added = await store.create_epic("Added")
    ids = [e["id"] for e in await store.list_epics()]
    assert ids == [added["id"], second["id"], first["id"]]
