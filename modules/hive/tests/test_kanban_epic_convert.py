"""An epic changes type by naming the epic it will sit under."""
import pytest

import kanban_data as store


async def test_an_epic_becomes_a_feature_under_the_epic_it_is_given(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    target = await store.create_epic("Gnome Shell stability")
    parent = await store.create_epic("Desktop")
    changed = await store.change_type(target["id"], "feature", parent["id"])
    assert changed["epic_id"] == parent["id"]
    assert [f["id"] for f in await store.list_features()] == [target["id"]]


async def test_an_epic_can_change_type_with_no_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    target = await store.create_epic("Gnome Shell stability")
    changed = await store.change_type(target["id"], "feature")
    assert changed["epic_id"] is None


async def test_an_epic_cannot_sit_under_itself(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    target = await store.create_epic("Gnome Shell stability")
    with pytest.raises(ValueError, match="own parent"):
        await store.change_type(target["id"], "feature", target["id"])
