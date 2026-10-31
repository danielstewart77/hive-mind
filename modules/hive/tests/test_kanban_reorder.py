"""Order and reparenting behavior for Kanban children."""
import importlib

import pytest

import kanban_data as store


@pytest.fixture(autouse=True)
def fresh_store():
    importlib.reload(store)


async def test_full_backlog_numbers_follow_sibling_reorders():
    first = await store.create_epic("First")
    second = await store.create_epic("Second")
    feature = await store.create_feature(first["id"], "Feature")
    story = await store.create_story("Story", feature_id=feature["id"])
    numbers = await store.backlog_numbers()
    assert numbers == {first["id"]: "2", second["id"]: "1", feature["id"]: "2.1", story["id"]: "2.1.1"}

    await store.update_epic(first["id"], before_id=second["id"])
    numbers = await store.backlog_numbers()
    assert numbers[first["id"]] == "1"
    assert numbers[second["id"]] == "2"
    assert numbers[feature["id"]] == "1.1"
    assert numbers[story["id"]] == "1.1.1"


async def test_moving_feature_at_drop_position_reorders_target_and_source_siblings():
    source = await store.create_epic("Source")
    target = await store.create_epic("Target")
    first = await store.create_feature(source["id"], "First")
    moved = await store.create_feature(source["id"], "Moved")
    last = await store.create_feature(source["id"], "Last")
    target_first = await store.create_feature(target["id"], "Target first")
    target_last = await store.create_feature(target["id"], "Target last")
    child = await store.create_story("Child", feature_id=moved["id"])

    await store.update_feature(moved["id"], epic_id=target["id"], before_id=target_last["id"])
    data = await store.get_data()
    assert [f["id"] for f in data["features"] if f["epic_id"] == source["id"]] == [first["id"], last["id"]]
    assert [f["id"] for f in data["features"] if f["epic_id"] == target["id"]] == [target_first["id"], moved["id"], target_last["id"]]
    assert next(s for s in data["stories"] if s["id"] == child["id"])["feature_id"] == moved["id"]


async def test_moving_story_at_drop_position_and_reloading_preserves_order():
    epic = await store.create_epic("Epic")
    source = await store.create_feature(epic["id"], "Source")
    target = await store.create_feature(epic["id"], "Target")
    story = await store.create_story("Moved", feature_id=source["id"])
    first = await store.create_story("First", feature_id=target["id"])
    last = await store.create_story("Last", feature_id=target["id"])

    await store.update_story(story["id"], feature_id=target["id"], before_id=last["id"])
    importlib.reload(store)
    stories = [s["id"] for s in (await store.get_data())["stories"] if s.get("feature_id") == target["id"]]
    assert stories == [first["id"], story["id"], last["id"]]


async def test_moving_within_same_parent_inserts_before_requested_sibling():
    epic = await store.create_epic("Epic")
    features = [await store.create_feature(epic["id"], name) for name in ("A", "B", "C")]
    await store.update_feature(features[2]["id"], epic_id=epic["id"], before_id=features[0]["id"])
    assert [f["id"] for f in await store.list_features()] == [features[2]["id"], features[0]["id"], features[1]["id"]]


async def test_a_drop_target_must_be_a_sibling_of_the_destination():
    first_epic = await store.create_epic("First")
    second_epic = await store.create_epic("Second")
    feature = await store.create_feature(first_epic["id"], "Feature")
    other = await store.create_feature(second_epic["id"], "Other")
    with pytest.raises(ValueError, match="not a child"):
        await store.update_feature(feature["id"], epic_id=second_epic["id"], before_id=other["id"] + "invalid")
    assert (await store.list_features())[0]["epic_id"] == first_epic["id"]


async def test_moving_an_epic_before_another_reorders_the_epic_list():
    first = await store.create_epic("First")
    second = await store.create_epic("Second")
    third = await store.create_epic("Third")
    assert [e["id"] for e in await store.list_epics()] == [third["id"], second["id"], first["id"]]
    await store.update_epic(third["id"], before_id=first["id"])
    assert [e["id"] for e in await store.list_epics()] == [second["id"], third["id"], first["id"]]


async def test_a_childless_story_becoming_a_feature_moves_under_its_feature_epic():
    epic = await store.create_epic("Epic")
    feature = await store.create_feature(epic["id"], "Feature")
    story = await store.create_story("Story", feature_id=feature["id"])
    changed = await store.change_type(story["id"], "feature", epic["id"])
    assert changed["epic_id"] == epic["id"]
    assert [f["id"] for f in await store.list_features()] == [feature["id"], story["id"]]


async def test_a_feature_with_children_changes_type_and_its_children_keep_their_parent():
    epic = await store.create_epic("Epic")
    feature = await store.create_feature(epic["id"], "Feature")
    child = await store.create_story("Child", feature_id=feature["id"])
    await store.change_type(feature["id"], "story", epic["id"])
    assert (await store.item_relations(child["id"]))["parent"]["id"] == feature["id"]


async def test_a_story_becomes_a_feature_under_the_epic_it_sat_in_directly():
    epic = await store.create_epic("Epic")
    story = await store.create_story("Story", epic_id=epic["id"])
    changed = await store.change_type(story["id"], "feature")
    assert changed["epic_id"] == epic["id"]


async def test_a_bug_is_created_under_a_feature_with_the_bug_subtype():
    epic = await store.create_epic("Epic")
    feature = await store.create_feature(epic["id"], "Feature")
    bug = await store.create_story("Crash", feature_id=feature["id"], subtype="bug")
    assert bug["subtype"] == "bug"
    assert bug["feature_id"] == feature["id"]
    assert (await store.list_stories())[0]["subtype"] == "bug"


async def test_a_story_can_become_a_bug_and_back_without_moving():
    epic = await store.create_epic("Epic")
    story = await store.create_story("Story", epic_id=epic["id"])
    assert (await store.change_type(story["id"], "bug"))["subtype"] == "bug"
    assert (await store.change_type(story["id"], "story"))["subtype"] == "story"
    assert (await store.list_stories())[0]["epic_id"] == epic["id"]


async def test_a_feature_becomes_a_bug_under_the_epic_it_sat_in():
    epic = await store.create_epic("Epic")
    feature = await store.create_feature(epic["id"], "Feature")
    bug = await store.change_type(feature["id"], "bug")
    assert bug["subtype"] == "bug"
    assert bug["epic_id"] == epic["id"]


async def test_removing_a_story_parent_leaves_the_story_unassigned_and_listed():
    epic = await store.create_epic("Epic")
    feature = await store.create_feature(epic["id"], "Feature")
    story = await store.create_story("Story", feature_id=feature["id"])
    cleared = await store.update_story(story["id"], parent="none")
    assert "feature_id" not in cleared and "epic_id" not in cleared
    assert cleared["subtype"] == "story"
    assert [s["id"] for s in await store.list_stories() if "feature_id" not in s and "epic_id" not in s] == [story["id"]]


async def test_removing_a_feature_epic_leaves_the_feature_unassigned_but_still_a_feature():
    epic = await store.create_epic("Epic")
    feature = await store.create_feature(epic["id"], "Feature")
    cleared = await store.update_feature(feature["id"], parent="none")
    assert cleared["epic_id"] is None
    assert (await store.list_features())[0]["id"] == feature["id"]
