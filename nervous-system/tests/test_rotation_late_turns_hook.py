"""Late-turn handoff — rotation_check.py hook side.

The hook lives at ~/.claude/hooks/rotation_check.py (edit-IS-deploy, no
staging mirror), outside this repo. Loaded here by absolute path so the
pure late-turn functions are covered by the same pytest run as the NS
side. Skipped if the hook is not installed on this host.
"""
from __future__ import annotations

import importlib.util
import os

import pytest

_HOOK_PATH = os.path.expanduser("~/.claude/hooks/rotation_check.py")

pytestmark = pytest.mark.skipif(
    not os.path.isfile(_HOOK_PATH), reason="rotation_check.py hook not installed on this host"
)


def _load_hook():
    spec = importlib.util.spec_from_file_location("rotation_check_hook", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# _unanswered_user_turns
# ---------------------------------------------------------------------------

def test_unanswered_trailing_user_turn() -> None:
    rc = _load_hook()
    turns = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},  # never answered before /clear
    ]
    assert rc._unanswered_user_turns(turns) == [{"role": "user", "content": "q2"}]


def test_completed_late_turns_have_no_continuation() -> None:
    rc = _load_hook()
    turns = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    assert rc._unanswered_user_turns(turns) == []


def test_multiple_trailing_user_turns() -> None:
    rc = _load_hook()
    turns = [
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "user", "content": "q3"},
    ]
    assert rc._unanswered_user_turns(turns) == [
        {"role": "user", "content": "q2"},
        {"role": "user", "content": "q3"},
    ]


def test_empty_late_turns() -> None:
    rc = _load_hook()
    assert rc._unanswered_user_turns([]) == []


# ---------------------------------------------------------------------------
# _merge_state_delta
# ---------------------------------------------------------------------------

def test_merge_state_delta_unions_lists_and_overrides_next_step() -> None:
    rc = _load_hook()
    state = {
        "in_flight_work": ["wire endpoint"],
        "decisions_made": ["use session_turns"],
        "open_questions": [],
        "files_edited": [{"path": "a.py", "what_changed": "added fn"}],
        "next_step": "old next",
    }
    delta = {
        "in_flight_work": ["wire endpoint", "add tests"],  # dup dropped, new kept
        "decisions_made": [],
        "open_questions": ["ship today?"],
        "files_edited": [{"path": "b.py", "what_changed": "new"}],
        "next_step": "answer the late ask",
    }
    merged = rc._merge_state_delta(state, delta)
    assert merged["in_flight_work"] == ["wire endpoint", "add tests"]
    assert merged["open_questions"] == ["ship today?"]
    assert [f["path"] for f in merged["files_edited"]] == ["a.py", "b.py"]
    assert merged["next_step"] == "answer the late ask"
    # input not mutated
    assert state["next_step"] == "old next"


def test_merge_state_delta_empty_next_step_keeps_original() -> None:
    rc = _load_hook()
    state = {"next_step": "keep me"}
    merged = rc._merge_state_delta(state, {"next_step": "   "})
    assert merged["next_step"] == "keep me"


def test_merge_state_delta_none_delta_returns_state() -> None:
    rc = _load_hook()
    state = {"next_step": "x"}
    assert rc._merge_state_delta(state, None) is state


def test_merge_state_delta_dedups_files_by_path() -> None:
    rc = _load_hook()
    state = {"files_edited": [{"path": "a.py", "what_changed": "one"}]}
    delta = {"files_edited": [{"path": "a.py", "what_changed": "two"}]}
    merged = rc._merge_state_delta(state, delta)
    assert merged["files_edited"] == [{"path": "a.py", "what_changed": "one"}]


# ---------------------------------------------------------------------------
# _late_delta_refresh
# ---------------------------------------------------------------------------

def test_late_delta_refresh_calls_ollama_with_blob(monkeypatch) -> None:
    rc = _load_hook()
    captured = {}

    def fake_structured(cfg, prompt, schema, retries=1):
        captured["prompt"] = prompt
        captured["schema"] = schema
        return {"next_step": "do the late thing"}

    monkeypatch.setattr(rc, "_ollama_structured", fake_structured)
    out = rc._late_delta_refresh(
        {}, [{"role": "user", "content": "new instruction"}]
    )
    assert out == {"next_step": "do the late thing"}
    assert "new instruction" in captured["prompt"]
    assert captured["schema"] is rc.LATE_DELTA_SCHEMA


def test_late_delta_refresh_empty_returns_none() -> None:
    rc = _load_hook()
    assert rc._late_delta_refresh({}, []) is None
    # turns with no text content also short-circuit before any Ollama call
    assert rc._late_delta_refresh({}, [{"role": "user", "content": ""}]) is None


# ---------------------------------------------------------------------------
# _fetch_late_turns
# ---------------------------------------------------------------------------

def test_fetch_late_turns_builds_query_and_parses(monkeypatch) -> None:
    rc = _load_hook()
    import io
    import json

    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                {"session_id": "s1", "turns": [{"role": "user", "content": "late"}]}
            ).encode()

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        return FakeResp()

    monkeypatch.setattr(rc.urllib.request, "urlopen", fake_urlopen)
    cfg = {"COMMS_URL": "http://ns:8426", "COMMS_BEARER": "tok"}
    turns = rc._fetch_late_turns(
        cfg, owner_type="telegram", client_ref="123", since=1000.0
    )
    assert turns == [{"role": "user", "content": "late"}]
    assert "/sessions/late-turns?" in captured["url"]
    assert "client_type=telegram" in captured["url"]
    assert "since=1000.0" in captured["url"]
    assert captured["auth"] == "Bearer tok"


def test_fetch_late_turns_missing_owner_returns_empty() -> None:
    rc = _load_hook()
    assert rc._fetch_late_turns({"COMMS_URL": "x", "COMMS_BEARER": ""}, owner_type="", client_ref="c", since=0.0) == []


def test_fetch_late_turns_swallows_errors(monkeypatch) -> None:
    rc = _load_hook()

    def boom(req, timeout=0):
        raise OSError("connection refused")

    monkeypatch.setattr(rc.urllib.request, "urlopen", boom)
    cfg = {"COMMS_URL": "http://ns:8426", "COMMS_BEARER": ""}
    assert rc._fetch_late_turns(cfg, owner_type="telegram", client_ref="1", since=0.0) == []
