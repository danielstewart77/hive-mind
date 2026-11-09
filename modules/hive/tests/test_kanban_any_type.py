"""Any item can take any type, placed under a parent of equal or higher level."""
import pytest

import kanban_data as store


async def test_a_story_becomes_a_feature_under_a_chosen_epic(tmp_path, monkeypatch):
    epic = await store.create_epic("Epic")
    other = await store.create_epic("Other")
    story = await store.create_story("Story", epic_id=epic["id"])
    changed = await store.change_type(story["id"], "feature", other["id"])
    assert changed["epic_id"] == other["id"]
    assert [f["id"] for f in await store.list_features()] == [story["id"]]


async def test_a_story_can_take_a_story_parent_when_it_becomes_a_feature(tmp_path, monkeypatch):
    epic = await store.create_epic("Epic")
    story = await store.create_story("Story", epic_id=epic["id"])
    other_story = await store.create_story("Other", epic_id=epic["id"])
    changed = await store.change_type(story["id"], "feature", other_story["id"])
    assert (await store.item_relations(changed["id"]))["parent"]["id"] == other_story["id"]


async def test_an_epic_can_be_given_a_parent_when_it_changes_type(tmp_path, monkeypatch):
    epic = await store.create_epic("Epic")
    feature = await store.create_feature(epic["id"], "Feature")
    await store.change_type(feature["id"], "epic", epic["id"])
    assert (await store.item_relations(feature["id"]))["parent"]["id"] == epic["id"]
