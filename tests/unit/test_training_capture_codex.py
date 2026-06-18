"""Unit tests for the Codex CLI rollout consumer.

Covers rollout parsing into the normalized turn array (real tool names kept
verbatim, arguments decoded into ``input``), reasoning-item dropping,
developer/system message skipping, ``event_msg`` noise being ignored,
metadata extraction (model / version / system prompt), and the end-to-end
``capture_session`` upsert with ``harness="codex"``.
"""

from __future__ import annotations

import json

import pytest

from core.training_capture import HARNESS_CODEX, count_examples, get_example
from core.training_capture_codex import (
    build_example,
    capture_session,
    parse_transcript,
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
    tool outputs, a reasoning item (dropped), and event_msg noise."""
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
        _message("assistant", "done"),
    ]
    p = tmp_path / "rollout-2026-06-18T06-54-27-019eda95.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return p


# ---------------------------------------------------------------------------
# parse_transcript
# ---------------------------------------------------------------------------

def test_parse_roles_in_order(rollout):
    turns = parse_transcript(rollout)
    roles = [t["role"] for t in turns]
    assert roles == [
        "user",       # "fix the thing"
        "assistant",  # "on it" + exec_command
        "tool",       # c1 result
        "assistant",  # "now patching" + apply_patch + mcp
        "tool",       # c2 result
        "assistant",  # "done"
    ]


def test_developer_message_skipped(rollout):
    turns = parse_transcript(rollout)
    blob = json.dumps(turns)
    assert "permissions" not in blob
    assert turns[0] == {"role": "user", "content": "fix the thing"}


def test_reasoning_dropped(rollout):
    turns = parse_transcript(rollout)
    assert all("secret reasoning" not in (t.get("content") or "") for t in turns)


def test_single_tool_call_attaches_to_assistant_turn(rollout):
    turns = parse_transcript(rollout)
    assistant = turns[1]
    assert assistant["content"] == "on it"
    assert assistant["tool_calls"] == [
        {"type": "exec_command", "input": {"cmd": "ls"}, "id": "c1"}
    ]


def test_batched_tool_calls_share_one_turn_with_real_names(rollout):
    turns = parse_transcript(rollout)
    batched = turns[3]
    assert batched["content"] == "now patching"
    types = [c["type"] for c in batched["tool_calls"]]
    assert types == ["apply_patch", "mcp__hive-mind-tools__graph_query"]
    assert batched["tool_calls"][1]["input"]["query"] == "MATCH (n) RETURN n"


def test_tool_output_links_call_id(rollout):
    turns = parse_transcript(rollout)
    assert turns[2] == {"role": "tool", "content": "file.txt", "tool_call_id": "c1"}
    assert turns[4] == {"role": "tool", "content": "patched", "tool_call_id": "c2"}


def test_event_msg_lines_ignored(rollout):
    turns = parse_transcript(rollout)
    assert all(t.get("content") != "token_count" for t in turns)


def test_tool_call_without_preceding_message_opens_new_turn(tmp_path):
    """A model that jumps straight to a tool gets a fresh assistant turn."""
    lines = [
        _message("user", "go"),
        _call("exec_command", {"cmd": "pwd"}, "x1"),
    ]
    p = tmp_path / "r.jsonl"
    p.write_text("\n".join(lines) + "\n")
    turns = parse_transcript(p)
    assert turns[1] == {
        "role": "assistant", "content": "",
        "tool_calls": [{"type": "exec_command", "input": {"cmd": "pwd"}, "id": "x1"}],
    }


def test_unparseable_arguments_preserved_raw(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text(_call("shell", "{not valid json", "y1") + "\n")
    turns = parse_transcript(p)
    assert turns[0]["tool_calls"][0]["input"] == {"raw": "{not valid json"}


def test_dict_output_flattened(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text(
        _call("exec_command", {"cmd": "ls"}, "z1") + "\n"
        + _output("z1", {"output": "wrapped text", "metadata": {"exit": 0}}) + "\n"
    )
    turns = parse_transcript(p)
    tool_turn = next(t for t in turns if t["role"] == "tool")
    assert tool_turn["content"] == "wrapped text"


def test_missing_file_returns_empty(tmp_path):
    assert parse_transcript(tmp_path / "nope.jsonl") == []


# ---------------------------------------------------------------------------
# build_example
# ---------------------------------------------------------------------------

def test_build_example_counts_and_metadata(rollout):
    ex = build_example(rollout, session_id="abc", mind_id="mind-uuid")
    assert ex is not None
    assert ex.session_id == "abc"
    assert ex.mind_id == "mind-uuid"
    assert ex.harness == HARNESS_CODEX
    assert ex.source_model == "gpt-5.4"
    assert ex.harness_version == "0.135.0"
    assert ex.system_prompt == "You are Codex, a coding agent."
    assert ex.turn_count == 6
    assert ex.tool_call_count == 3  # exec_command + apply_patch + mcp
    assert ex.captured_at is not None


def test_build_example_empty_rollout_returns_none(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    assert build_example(p, session_id="x") is None


# ---------------------------------------------------------------------------
# capture_session
# ---------------------------------------------------------------------------

def test_capture_session_upserts(rollout, tmp_path):
    db = tmp_path / "data" / "training_examples.db"
    assert capture_session(rollout, session_id="abc", mind_id="m", db_path=db) is True
    assert count_examples(db, harness=HARNESS_CODEX) == 1
    row = get_example(db, "abc")
    assert row["harness"] == HARNESS_CODEX
    assert row["tool_call_count"] == 3
    assert len(row["turns"]) == 6


def test_capture_session_idempotent(rollout, tmp_path):
    db = tmp_path / "training_examples.db"
    capture_session(rollout, session_id="abc", db_path=db)
    capture_session(rollout, session_id="abc", db_path=db)
    assert count_examples(db) == 1


def test_capture_session_noop_on_empty(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("\n")
    db = tmp_path / "training_examples.db"
    assert capture_session(p, session_id="x", db_path=db) is False
    assert not db.exists()
