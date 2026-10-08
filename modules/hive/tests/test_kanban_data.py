import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import kanban_data as store


def setup_function():
    store.reset()


def test_seed_counts_and_statuses():
    data = store.get_data()
    assert [len(data[k]) for k in ("epics", "features", "stories")] == [4, 9, 14]
    assert data["epics"] == [
        {"id": "e1", "title": "Board experience"},
        {"id": "e2", "title": "Backlog & work items"},
        {"id": "e3", "title": "Integrations"},
        {"id": "e4", "title": "Platform & polish"},
    ]
    assert data["features"] == [
        {"id": "f1", "epic_id": "e1", "title": "Lane interactions"},
        {"id": "f2", "epic_id": "e1", "title": "Card design"},
        {"id": "f3", "epic_id": "e1", "title": "Keyboard access"},
        {"id": "f4", "epic_id": "e2", "title": "Work item model"},
        {"id": "f5", "epic_id": "e2", "title": "Bulk editing"},
        {"id": "f6", "epic_id": "e3", "title": "Hive Mind sync"},
        {"id": "f7", "epic_id": "e3", "title": "Calendar feed"},
        {"id": "f8", "epic_id": "e4", "title": "Theming"},
        {"id": "f9", "epic_id": "e4", "title": "Performance"},
    ]
    assert data["stories"] == [
        {"id": "s1", "feature_id": "f1", "title": "Drop placeholder while dragging", "status": "committed"},
        {"id": "s2", "feature_id": "f1", "title": "Snap card back on failed drop", "status": "in_progress"},
        {"id": "s3", "feature_id": "f2", "title": "Card hover elevation", "status": "done"},
        {"id": "s4", "feature_id": "f2", "title": "Parent chips on cards", "status": "done"},
        {"id": "s5", "feature_id": "f4", "title": "Story point estimates", "status": "backlog"},
        {"id": "s6", "feature_id": "f4", "title": "Archive finished stories", "status": "backlog"},
        {"id": "s7", "feature_id": "f5", "title": "Multi-select with shift-click", "status": "backlog"},
        {"id": "s8", "feature_id": "f6", "title": "Two-way status sync", "status": "in_progress"},
        {"id": "s9", "feature_id": "f6", "title": "OAuth handshake", "status": "backlog"},
        {"id": "s10", "feature_id": "f7", "title": "ICS export of committed work", "status": "committed"},
        {"id": "s11", "feature_id": "f8", "title": "Custom accent color", "status": "backlog"},
        {"id": "s12", "feature_id": "f9", "title": "Lazy-render long lanes", "status": "committed"},
        {"id": "s13", "epic_id": "e3", "title": "Slack notifications", "status": "in_progress"},
        {"id": "s14", "epic_id": "e4", "title": "Empty-state illustrations", "status": "done"},
    ]
    assert {s["status"] for s in data["stories"]} == set(store.STATUSES)


def test_reset_and_returned_values_are_detached():
    snapshot = store.get_data()
    snapshot["epics"][0]["title"] = "changed"
    store.create_epic("Extra")
    reset = store.reset()
    reset["epics"].clear()
    assert len(store.get_data()["epics"]) == 4
    assert store.get_data()["epics"][0]["title"] == "Board experience"


def test_crud_and_ids():
    e = store.create_epic("New epic")
    f = store.create_feature(e["id"], "New feature")
    s = store.create_story("New story", feature_id=f["id"])
    assert (e["id"], f["id"], s["id"], s["status"]) == ("e5", "f10", "s15", "backlog")
    assert store.update_epic(e["id"], title="Changed") ["title"] == "Changed"
    assert store.update_feature(f["id"], title="Changed feature")["title"] == "Changed feature"
    assert store.update_story(s["id"], status="done")["status"] == "done"
    assert store.delete_story(s["id"])["id"] == s["id"]
    assert store.delete_feature(f["id"])["id"] == f["id"]
    assert store.delete_epic(e["id"])["id"] == e["id"]


def test_validation_and_missing_records():
    with pytest.raises(ValueError, match="title must be a nonblank string"):
        store.create_epic("  ")
    with pytest.raises(KeyError, match="Epic not found: missing"):
        store.create_feature("missing", "Feature")
    with pytest.raises(ValueError, match="exactly one"):
        store.create_story("Story")
    with pytest.raises(ValueError, match="exactly one"):
        store.create_story("Story", feature_id="f1", epic_id="e1")
    with pytest.raises(KeyError, match="Feature not found: missing"):
        store.create_story("Story", feature_id="missing")
    with pytest.raises(ValueError, match="unsupported status: unknown"):
        store.create_story("Story", epic_id="e1", status="unknown")
    with pytest.raises(KeyError, match="Story not found: missing"):
        store.update_story("missing", title="x")
    with pytest.raises(ValueError, match="exactly one"):
        store.update_story("s1", epic_id="e1")
    with pytest.raises(ValueError, match="unsupported fields: unknown"):
        store.update_epic("e1", unknown="x")


def test_generated_ids_remain_unique_after_deletion():
    first = store.create_epic("First")
    store.delete_epic(first["id"])
    second = store.create_epic("Second")
    assert second["id"] == "e6"
    store.reset()
    assert store.create_epic("After reset")["id"] == "e5"


def test_epic_and_feature_cascades():
    result = store.delete_epic("e3")
    assert result["id"] == "e3"
    data = store.get_data()
    assert not {"f6", "f7"} & {f["id"] for f in data["features"]}
    assert not {"s8", "s9", "s10", "s13"} & {s["id"] for s in data["stories"]}
    store.delete_feature("f1")
    assert not {"s1", "s2"} & {s["id"] for s in store.get_data()["stories"]}
