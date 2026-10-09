"""Any item can take any type, placed under a parent of equal or higher level."""
import pytest

import kanban_data as store


def test_a_story_becomes_a_feature_under_a_chosen_epic(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    epic = store.create_epic("Epic")
    other = store.create_epic("Other")
    story = store.create_story("Story", epic_id=epic["id"])
    changed = store.change_type(story["id"], "feature", other["id"])
    assert changed["epic_id"] == other["id"]
    assert [f["id"] for f in store.list_features()] == [story["id"]]


def test_a_story_can_take_a_story_parent_when_it_becomes_a_feature(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    epic = store.create_epic("Epic")
    story = store.create_story("Story", epic_id=epic["id"])
    other_story = store.create_story("Other", epic_id=epic["id"])
    changed = store.change_type(story["id"], "feature", other_story["id"])
    assert store.item_relations(changed["id"])["parent"]["id"] == other_story["id"]


def test_an_epic_can_be_given_a_parent_when_it_changes_type(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    epic = store.create_epic("Epic")
    feature = store.create_feature(epic["id"], "Feature")
    store.change_type(feature["id"], "epic", epic["id"])
    assert store.item_relations(feature["id"])["parent"]["id"] == epic["id"]
