"""Resettable in-memory Kanban data store."""
from copy import deepcopy

STATUSES = ("backlog", "committed", "in_progress", "done")

_SEED = {
    "epics": [
        {"id": "e1", "title": "Board experience"},
        {"id": "e2", "title": "Backlog & work items"},
        {"id": "e3", "title": "Integrations"},
        {"id": "e4", "title": "Platform & polish"},
    ],
    "features": [
        {"id": "f1", "epic_id": "e1", "title": "Lane interactions"},
        {"id": "f2", "epic_id": "e1", "title": "Card design"},
        {"id": "f3", "epic_id": "e1", "title": "Keyboard access"},
        {"id": "f4", "epic_id": "e2", "title": "Work item model"},
        {"id": "f5", "epic_id": "e2", "title": "Bulk editing"},
        {"id": "f6", "epic_id": "e3", "title": "Hive Mind sync"},
        {"id": "f7", "epic_id": "e3", "title": "Calendar feed"},
        {"id": "f8", "epic_id": "e4", "title": "Theming"},
        {"id": "f9", "epic_id": "e4", "title": "Performance"},
    ],
    "stories": [
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
    ],
}

_data = deepcopy(_SEED)
_counters = {"epics": 5, "features": 10, "stories": 15}
_PREFIX = {"epics": "e", "features": "f", "stories": "s"}


def reset():
    """Restore seed data and ID sequences; return a detached snapshot."""
    global _data, _counters
    _data = deepcopy(_SEED)
    _counters = {"epics": 5, "features": 10, "stories": 15}
    return get_data()


def get_data():
    return deepcopy(_data)


def list_epics(): return deepcopy(_data["epics"])
def list_features(): return deepcopy(_data["features"])
def list_stories(): return deepcopy(_data["stories"])


def _collection(kind):
    if kind not in _data:
        raise ValueError(f"Unknown entity type: {kind}")
    return _data[kind]


def _title(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("title must be a nonblank string")
    return value.strip()


def _find(kind, record_id):
    for row in _collection(kind):
        if row["id"] == record_id:
            return row
    label = {"epics": "Epic", "features": "Feature", "stories": "Story"}[kind]
    raise KeyError(f"{label} not found: {record_id}")


def _create(kind, fields):
    row = dict(fields)
    row["id"] = _PREFIX[kind] + str(_counters[kind])
    _counters[kind] += 1
    _collection(kind).append(row)
    return deepcopy(row)


def create_epic(title):
    return _create("epics", {"title": _title(title)})


def create_feature(epic_id, title):
    _find("epics", epic_id)
    return _create("features", {"epic_id": epic_id, "title": _title(title)})


def create_story(title, feature_id=None, epic_id=None, status="backlog"):
    if (feature_id is None) == (epic_id is None):
        raise ValueError("story requires exactly one of feature_id or epic_id")
    if feature_id is not None:
        _find("features", feature_id)
    else:
        _find("epics", epic_id)
    if status not in STATUSES:
        raise ValueError(f"unsupported status: {status}")
    return _create("stories", {"title": _title(title), "status": status,
                                **({"feature_id": feature_id} if feature_id is not None else {"epic_id": epic_id})})


def _update(kind, record_id, changes):
    allowed = {"epics": {"title"}, "features": {"title", "epic_id"},
               "stories": {"title", "status", "feature_id", "epic_id"}}[kind]
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"unsupported fields: {', '.join(sorted(unknown))}")
    row = _find(kind, record_id)
    candidate = dict(row)
    candidate.update(changes)
    if "title" in candidate:
        candidate["title"] = _title(candidate["title"])
    if kind == "features":
        _find("epics", candidate["epic_id"])
    if kind == "stories":
        has_feature, has_epic = candidate.get("feature_id") is not None, candidate.get("epic_id") is not None
        if has_feature == has_epic:
            raise ValueError("story requires exactly one of feature_id or epic_id")
        _find("features" if has_feature else "epics", candidate.get("feature_id") if has_feature else candidate.get("epic_id"))
        if candidate.get("status") not in STATUSES:
            raise ValueError(f"unsupported status: {candidate.get('status')}")
    row.clear()
    row.update(candidate)
    return deepcopy(row)


def update_epic(record_id, **changes): return _update("epics", record_id, changes)
def update_feature(record_id, **changes): return _update("features", record_id, changes)
def update_story(record_id, **changes): return _update("stories", record_id, changes)


def _delete(kind, record_id):
    row = _find(kind, record_id)
    if kind == "epics":
        feature_ids = {f["id"] for f in _data["features"] if f["epic_id"] == record_id}
        _data["stories"][:] = [s for s in _data["stories"] if s.get("epic_id") != record_id and s.get("feature_id") not in feature_ids]
        _data["features"][:] = [f for f in _data["features"] if f["epic_id"] != record_id]
    elif kind == "features":
        _data["stories"][:] = [s for s in _data["stories"] if s.get("feature_id") != record_id]
    _collection(kind).remove(row)
    return deepcopy(row)


def delete_epic(record_id): return _delete("epics", record_id)
def delete_feature(record_id): return _delete("features", record_id)
def delete_story(record_id): return _delete("stories", record_id)
