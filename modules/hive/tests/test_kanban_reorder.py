"""Order and reparenting behavior for Kanban children."""
import importlib

import pytest

import kanban_data as store


@pytest.fixture(autouse=True)
def fresh_store():
    importlib.reload(store)


def test_moving_feature_at_drop_position_reorders_target_and_source_siblings():
    source = store.create_epic("Source")
    target = store.create_epic("Target")
    first = store.create_feature(source["id"], "First")
    moved = store.create_feature(source["id"], "Moved")
    last = store.create_feature(source["id"], "Last")
    target_first = store.create_feature(target["id"], "Target first")
    target_last = store.create_feature(target["id"], "Target last")
    child = store.create_story("Child", feature_id=moved["id"])

    store.update_feature(moved["id"], epic_id=target["id"], before_id=target_last["id"])
    data = store.get_data()
    assert [f["id"] for f in data["features"] if f["epic_id"] == source["id"]] == [first["id"], last["id"]]
    assert [f["id"] for f in data["features"] if f["epic_id"] == target["id"]] == [target_first["id"], moved["id"], target_last["id"]]
    assert next(s for s in data["stories"] if s["id"] == child["id"])["feature_id"] == moved["id"]


def test_moving_story_at_drop_position_and_reloading_preserves_order():
    epic = store.create_epic("Epic")
    source = store.create_feature(epic["id"], "Source")
    target = store.create_feature(epic["id"], "Target")
    story = store.create_story("Moved", feature_id=source["id"])
    first = store.create_story("First", feature_id=target["id"])
    last = store.create_story("Last", feature_id=target["id"])

    store.update_story(story["id"], feature_id=target["id"], before_id=last["id"])
    importlib.reload(store)
    stories = [s["id"] for s in store.get_data()["stories"] if s.get("feature_id") == target["id"]]
    assert stories == [first["id"], story["id"], last["id"]]


def test_moving_within_same_parent_inserts_before_requested_sibling():
    epic = store.create_epic("Epic")
    features = [store.create_feature(epic["id"], name) for name in ("A", "B", "C")]
    store.update_feature(features[2]["id"], epic_id=epic["id"], before_id=features[0]["id"])
    assert [f["id"] for f in store.list_features()] == [features[2]["id"], features[0]["id"], features[1]["id"]]


def test_a_drop_target_must_be_a_sibling_of_the_destination():
    first_epic = store.create_epic("First")
    second_epic = store.create_epic("Second")
    feature = store.create_feature(first_epic["id"], "Feature")
    other = store.create_feature(second_epic["id"], "Other")
    with pytest.raises(ValueError, match="not a child"):
        store.update_feature(feature["id"], epic_id=second_epic["id"], before_id=other["id"] + "invalid")
    assert store.list_features()[0]["epic_id"] == first_epic["id"]


def test_moving_an_epic_before_another_reorders_the_epic_list():
    first = store.create_epic("First")
    second = store.create_epic("Second")
    third = store.create_epic("Third")
    store.update_epic(third["id"], before_id=first["id"])
    assert [e["id"] for e in store.list_epics()] == [third["id"], first["id"], second["id"]]


def test_a_childless_story_becoming_a_feature_moves_under_its_feature_epic():
    epic = store.create_epic("Epic")
    feature = store.create_feature(epic["id"], "Feature")
    story = store.create_story("Story", feature_id=feature["id"])
    changed = store.change_type(story["id"], "feature")
    assert changed["epic_id"] == epic["id"]
    assert [f["id"] for f in store.list_features()] == [feature["id"], story["id"]]


def test_a_feature_with_children_cannot_change_type():
    epic = store.create_epic("Epic")
    feature = store.create_feature(epic["id"], "Feature")
    store.create_story("Child", feature_id=feature["id"])
    with pytest.raises(ValueError, match="children"):
        store.change_type(feature["id"], "story")
    assert store.list_features()[0]["id"] == feature["id"]


def test_a_story_becomes_a_feature_under_the_epic_it_sat_in_directly():
    epic = store.create_epic("Epic")
    story = store.create_story("Story", epic_id=epic["id"])
    changed = store.change_type(story["id"], "feature")
    assert changed["epic_id"] == epic["id"]


def test_a_bug_is_created_under_a_feature_with_the_bug_subtype():
    epic = store.create_epic("Epic")
    feature = store.create_feature(epic["id"], "Feature")
    bug = store.create_story("Crash", feature_id=feature["id"], subtype="bug")
    assert bug["subtype"] == "bug"
    assert bug["feature_id"] == feature["id"]
    assert store.list_stories()[0]["subtype"] == "bug"


def test_a_story_can_become_a_bug_and_back_without_moving():
    epic = store.create_epic("Epic")
    story = store.create_story("Story", epic_id=epic["id"])
    assert store.change_type(story["id"], "bug")["subtype"] == "bug"
    assert store.change_type(story["id"], "story")["subtype"] == "story"
    assert store.list_stories()[0]["epic_id"] == epic["id"]


def test_a_feature_cannot_become_a_bug():
    epic = store.create_epic("Epic")
    feature = store.create_feature(epic["id"], "Feature")
    with pytest.raises(ValueError, match="Only a story"):
        store.change_type(feature["id"], "bug")


def test_removing_a_story_parent_leaves_the_story_unassigned_and_listed():
    epic = store.create_epic("Epic")
    feature = store.create_feature(epic["id"], "Feature")
    story = store.create_story("Story", feature_id=feature["id"])
    cleared = store.update_story(story["id"], parent="none")
    assert "feature_id" not in cleared and "epic_id" not in cleared
    assert cleared["subtype"] == "story"
    assert [s["id"] for s in store.list_stories() if "feature_id" not in s and "epic_id" not in s] == [story["id"]]


def test_removing_a_feature_epic_leaves_the_feature_unassigned_but_still_a_feature():
    epic = store.create_epic("Epic")
    feature = store.create_feature(epic["id"], "Feature")
    cleared = store.update_feature(feature["id"], parent="none")
    assert cleared["epic_id"] is None
    assert store.list_features()[0]["id"] == feature["id"]
