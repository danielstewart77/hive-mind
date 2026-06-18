"""Unit tests for the Codex CLI rollout consumer.

Covers grouping the rollout into per-turn rows, the assistant block sequence
(text / tool_use / tool_result in order, arguments decoded into ``input``),
reasoning-item dropping (has_reasoning always False), developer/system
message skipping, ``event_msg`` noise being ignored, metadata extraction
(model / version / system prompt), and the end-to-end ``capture_session``
upsert with ``harness="codex"``.
"""

from __future__ import annotations

import json

import pytest

from core.training_capture import HARNESS_CODEX, count_turns, get_turns
from core.training_capture_codex import (
    build_turns,
    capture_session,
    _parse_grouped,
)


def _line(type_, payload) -> str:
    return json.dumps({"type": type_, "payload": payload})


def _item(payload) -> str:
    return _line("response_item", payload)


def _message(role, text) -> str:
    block_type = "output_text" if role == "assistant" else "input_text"
    return _item({"type": "message", "role": role,
                  "content": [{"type": block_type, "text": text}]})


def _call(name, args, call_id) -> str:
    arguments = args if isinstance(args, str) else json.dumps(args)
    return _item({"type": "function_call", "name": name,
                  "arguments": arguments, "call_id": call_id})


def _output(call_id, output) -> str:
    return _item({"type": "function_call_output", "call_id": call_id,
                  "output": output})


@pytest.fixture
def rollout(tmp_path):
    """A representative rollout: meta + model context, a developer preamble
    (skipped), real user/assistant turns, single and batched tool calls,
    tool outputs, a reasoning item (dropped), and event_msg noise. Two human
    turns."""
    lines = [
        _line("session_meta", {
            "id": "019eda95-codex",
            "cli_version": "0.135.0",
            "base_instructions": {"text": "You are Codex, a coding agent."},
        }),
        _line("turn_context", {"model": "gpt-5.4"}),
        _line("event_msg", {"type": "token_count"}),  # noise, ignored
        # developer scaffolding message — skipped from turns
        _item({"type": "message", "role": "developer",
               "content": [{"type": "input_text", "text": "<permissions ...>"}]}),
        _message("user", "fix the thing"),
        _message("assistant", "on it"),
        _call("exec_command", {"cmd": "ls"}, "c1"),
        _output("c1", "file.txt"),
        _message("assistant", "now patching"),
        _call("apply_patch", {"patch": "@@"}, "c2"),
        _call("mcp__hive-mind-tools__graph_query", {"query": "MATCH (n) RETURN n"}, "c3"),
        _output("c2", "patched"),
        # reasoning item must be dropped entirely
        _item({"type": "reasoning", "summary": [],
               "encrypted_content": "secret reasoning, must be dropped"}),
        # second human turn
        _message("user", "now ship it"),
        _message("assistant", "done"),
    ]
    p = tmp_path / "rollout-2026-06-18T06-54-27-019eda95.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return p


# ---------------------------------------------------------------------------
# turn grouping
# ---------------------------------------------------------------------------

def test_groups_into_two_human_turns(rollout):
    grouped = _parse_grouped(rollout)
    assert len(grouped) == 2
    assert grouped[0][0] == "fix the thing"
    assert grouped[1][0] == "now ship it"


def test_first_turn_block_sequence(rollout):
    grouped = _parse_grouped(rollout)
    block_types = [b["type"] for b in grouped[0][1]]
    assert block_types == [
        "text",         # "on it"
        "tool_use",     # exec_command
        "tool_result",  # c1
        "text",         # "now patching"
        "tool_use",     # apply_patch
        "tool_use",     # mcp
        "tool_result",  # c2
    ]


def test_developer_message_skipped(rollout):
    grouped = _parse_grouped(rollout)
    blob = json.dumps(grouped)
    assert "permissions" not in blob


def test_reasoning_dropped(rollout):
    grouped = _parse_grouped(rollout)
    blob = json.dumps(grouped)
    assert "secret reasoning" not in blob


def test_tool_use_blocks_decoded_and_named(rollout):
    grouped = _parse_grouped(rollout)
    tool_uses = [b for b in grouped[0][1] if b["type"] == "tool_use"]
    names = [b["name"] for b in tool_uses]
    assert names == ["exec_command", "apply_patch", "mcp__hive-mind-tools__graph_query"]
    assert tool_uses[0] == {
        "type": "tool_use", "name": "exec_command", "input": {"cmd": "ls"}, "id": "c1"
    }
    assert tool_uses[2]["input"]["query"] == "MATCH (n) RETURN n"


def test_tool_result_links_call_id(rollout):
    grouped = _parse_grouped(rollout)
    results = [b for b in grouped[0][1] if b["type"] == "tool_result"]
    assert results[0] == {
        "type": "tool_result", "content": "file.txt", "tool_call_id": "c1"
    }
    assert results[1] == {
        "type": "tool_result", "content": "patched", "tool_call_id": "c2"
    }


def test_event_msg_lines_ignored(rollout):
    grouped = _parse_grouped(rollout)
    blob = json.dumps(grouped)
    assert "token_count" not in blob


def test_tool_call_before_first_user_attaches_to_leading_turn(tmp_path):
    """A tool call with no preceding user message attaches to a leading turn
    with empty user_content."""
    p = tmp_path / "r.jsonl"
    p.write_text(_call("exec_command", {"cmd": "pwd"}, "x1") + "\n")
    grouped = _parse_grouped(p)
    assert grouped[0][0] == ""
    assert grouped[0][1] == [
        {"type": "tool_use", "name": "exec_command", "input": {"cmd": "pwd"}, "id": "x1"}
    ]


def test_unparseable_arguments_preserved_raw(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text(
        _message("user", "go") + "\n"
        + _call("shell", "{not valid json", "y1") + "\n"
    )
    grouped = _parse_grouped(p)
    tool_use = grouped[0][1][0]
    assert tool_use["input"] == {"raw": "{not valid json"}


def test_dict_output_flattened(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text(
        _message("user", "go") + "\n"
        + _call("exec_command", {"cmd": "ls"}, "z1") + "\n"
        + _output("z1", {"output": "wrapped text", "metadata": {"exit": 0}}) + "\n"
    )
    grouped = _parse_grouped(p)
    result = next(b for b in grouped[0][1] if b["type"] == "tool_result")
    assert result["content"] == "wrapped text"


def test_missing_file_returns_empty(tmp_path):
    assert _parse_grouped(tmp_path / "nope.jsonl") == []


# ---------------------------------------------------------------------------
# build_turns
# ---------------------------------------------------------------------------

def test_build_turns_metadata_and_indices(rollout):
    turns = build_turns(rollout, session_id="abc", mind_id="mind-uuid")
    assert len(turns) == 2
    assert [t.turn_index for t in turns] == [0, 1]
    first = turns[0]
    assert first.session_id == "abc"
    assert first.mind_id == "mind-uuid"
    assert first.harness == HARNESS_CODEX
    assert first.source_model == "gpt-5.4"
    assert first.harness_version == "0.135.0"
    assert first.system_prompt == "You are Codex, a coding agent."
    assert first.has_reasoning is False  # Codex never carries reasoning
    assert first.tool_call_count == 3  # exec_command + apply_patch + mcp
    assert first.captured_at is not None
    # system prompt denormalized onto every row
    assert turns[1].system_prompt == "You are Codex, a coding agent."


def test_build_turns_always_no_reasoning(rollout):
    turns = build_turns(rollout, session_id="abc")
    assert all(t.has_reasoning is False for t in turns)


def test_build_turns_empty_rollout_returns_empty(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    assert build_turns(p, session_id="x") == []


# ---------------------------------------------------------------------------
# capture_session
# ---------------------------------------------------------------------------

def test_capture_session_upserts(rollout, tmp_path):
    db = tmp_path / "data" / "training_turns.db"
    assert capture_session(rollout, session_id="abc", mind_id="m", db_path=db) is True
    assert count_turns(db, harness=HARNESS_CODEX) == 2
    rows = get_turns(db, "abc")
    assert rows[0]["harness"] == HARNESS_CODEX
    assert rows[0]["tool_call_count"] == 3
    assert rows[0]["has_reasoning"] is False


def test_capture_session_idempotent(rollout, tmp_path):
    db = tmp_path / "training_turns.db"
    capture_session(rollout, session_id="abc", db_path=db)
    capture_session(rollout, session_id="abc", db_path=db)
    assert count_turns(db) == 2


def test_capture_session_noop_on_empty(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("\n")
    db = tmp_path / "training_turns.db"
    assert capture_session(p, session_id="x", db_path=db) is False
    assert not db.exists()
