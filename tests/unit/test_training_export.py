"""Unit tests for rendering curated turns into training JSONL.

Covers block-array to message-list translation, parallel tool-call grouping,
reasoning placement in both modes, the three-way credential policy,
tool-result capping, and the session-level train/eval split.
"""

from __future__ import annotations

import json

import pytest

from core.training_capture import (
    HARNESS_CLAUDE_CODE,
    HARNESS_CODEX,
    TrainingTurn,
    init_db,
    upsert_turn,
)
from core.training_curation import CurationPolicy, curate
from core.training_export import (
    MODE_REASONING,
    MODE_STRIPPED,
    SECRETS_RANDOMIZE,
    SECRETS_REDACT,
    ExportOptions,
    export_dataset,
    render_turn,
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "training_turns.db"
    init_db(path)
    return path


def _blocks():
    return [
        {"type": "thinking", "text": "check the listing first"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}, "id": "t1"},
        {"type": "tool_result", "content": "a.txt", "tool_call_id": "t1"},
        {"type": "thinking", "text": "now read it"},
        {"type": "tool_use", "name": "Read", "input": {"path": "a.txt"}, "id": "t2"},
        {"type": "tool_result", "content": "hello", "tool_call_id": "t2"},
        {"type": "text", "text": "The file says hello."},
    ]


def _add(db_path, session, index=0, blocks=None, user="look at a.txt", **kwargs):
    upsert_turn(
        db_path,
        TrainingTurn.from_blocks(
            session_id=session,
            turn_index=index,
            harness=kwargs.pop("harness", HARNESS_CLAUDE_CODE),
            user_content=user,
            assistant_blocks=_blocks() if blocks is None else blocks,
            system_prompt=kwargs.pop("system_prompt", "You are a mind."),
            **kwargs,
        ),
    )


def _read(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_render_produces_alternating_roles():
    messages = render_turn("go", _blocks(), ExportOptions(mode=MODE_STRIPPED))
    assert [m["role"] for m in messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]


def test_tool_calls_carry_name_and_json_arguments():
    messages = render_turn("go", _blocks(), ExportOptions())
    call = messages[1]["tool_calls"][0]
    assert call["function"]["name"] == "Bash"
    assert json.loads(call["function"]["arguments"]) == {"command": "ls"}
    assert call["id"] == "t1"


def test_tool_result_replies_to_its_call():
    messages = render_turn("go", _blocks(), ExportOptions())
    assert messages[2]["tool_call_id"] == "t1"
    assert messages[2]["content"] == "a.txt"


def test_parallel_calls_stay_in_one_assistant_message():
    """The harness batches independent calls; flattening would unteach that."""
    blocks = [
        {"type": "tool_use", "name": "Read", "input": {"path": "a"}, "id": "t1"},
        {"type": "tool_use", "name": "Read", "input": {"path": "b"}, "id": "t2"},
        {"type": "tool_result", "content": "A", "tool_call_id": "t1"},
        {"type": "tool_result", "content": "B", "tool_call_id": "t2"},
        {"type": "text", "text": "read both"},
    ]
    messages = render_turn("go", blocks, ExportOptions())
    assert len(messages[1]["tool_calls"]) == 2


def test_stripped_mode_drops_reasoning_entirely():
    messages = render_turn("go", _blocks(), ExportOptions(mode=MODE_STRIPPED))
    serialized = json.dumps(messages)
    assert "reasoning" not in serialized
    assert "check the listing first" not in serialized


def test_reasoning_mode_keeps_thoughts_with_the_action_they_produced():
    messages = render_turn("go", _blocks(), ExportOptions(mode=MODE_REASONING))
    first = messages[1]
    second = messages[3]
    assert first["reasoning"] == "check the listing first"
    assert first["tool_calls"][0]["function"]["name"] == "Bash"
    assert second["reasoning"] == "now read it"
    assert second["tool_calls"][0]["function"]["name"] == "Read"


def test_no_empty_reasoning_key_when_a_group_has_no_thought():
    blocks = [
        {"type": "tool_use", "name": "Bash", "input": {}, "id": "t1"},
        {"type": "tool_result", "content": "ok", "tool_call_id": "t1"},
        {"type": "text", "text": "done"},
    ]
    messages = render_turn("go", blocks, ExportOptions(mode=MODE_REASONING))
    assert all("reasoning" not in m for m in messages)


def _credential_blocks():
    return [
        {"type": "tool_use", "name": "Bash", "input": {"command": "cat .env"}, "id": "t1"},
        {
            "type": "tool_result",
            "content": "GITHUB_TOKEN=ghp_AAAAbbbbCCCCddddEEEEffff",
            "tool_call_id": "t1",
        },
        {"type": "text", "text": "the key is sk-ant-api03-QQQQwwwwEEEErrrr1234"},
    ]


def test_credentials_survive_by_default():
    """The corpus trains a local model that needs the real values.

    Substituting placeholders teaches it that a redaction slug is what goes
    in the credential slot, so it emits one when it needs a live token.
    """
    serialized = json.dumps(
        render_turn("here is sk-ant-api03-ZZZZyyyyXXXXvvvv9999", _credential_blocks(), ExportOptions())
    )
    assert "ghp_AAAAbbbbCCCCddddEEEEffff" in serialized
    assert "sk-ant-api03-QQQQwwwwEEEErrrr1234" in serialized
    assert "sk-ant-api03-ZZZZyyyyXXXXvvvv9999" in serialized
    assert "REDACTED" not in serialized


def test_redaction_scrubs_everything_when_asked():
    """For a dataset leaving this machine."""
    serialized = json.dumps(
        render_turn(
            "here is sk-ant-api03-ZZZZyyyyXXXXvvvv9999",
            _credential_blocks(),
            ExportOptions(secrets=SECRETS_REDACT),
        )
    )
    assert "ghp_AAAAbbbb" not in serialized
    assert "sk-ant-api03-QQQQ" not in serialized
    assert "sk-ant-api03-ZZZZ" not in serialized
    assert "REDACTED" in serialized


def test_long_tool_results_are_truncated_with_a_marker():
    blocks = [
        {"type": "tool_use", "name": "Read", "input": {"path": "big"}, "id": "t1"},
        {"type": "tool_result", "content": "x" * 5_000, "tool_call_id": "t1"},
        {"type": "text", "text": "done"},
    ]
    messages = render_turn("go", blocks, ExportOptions(max_tool_result_chars=100))
    assert "characters truncated" in messages[2]["content"]
    assert len(messages[2]["content"]) < 200


def test_export_writes_only_kept_rows(db_path, tmp_path):
    _add(db_path, "keep-me")
    _add(db_path, "drop-me", user="[Request interrupted by user]")
    curate(db_path)
    report = export_dataset(db_path, tmp_path / "out")
    lines = _read(tmp_path / "out" / "train.jsonl") + _read(tmp_path / "out" / "eval.jsonl")
    assert report.train_examples + report.eval_examples == 1
    assert lines[0]["meta"]["session_id"] == "keep-me"


def test_system_prompt_is_included_and_can_be_omitted(db_path, tmp_path):
    _add(db_path, "s1")
    curate(db_path)

    export_dataset(db_path, tmp_path / "with", ExportOptions(eval_fraction=0.0))
    with_lines = _read(tmp_path / "with" / "train.jsonl")
    assert with_lines[0]["messages"][0]["role"] == "system"

    export_dataset(
        db_path,
        tmp_path / "without",
        ExportOptions(include_system_prompt=False, eval_fraction=0.0),
    )
    without_lines = _read(tmp_path / "without" / "train.jsonl")
    assert without_lines[0]["messages"][0]["role"] == "user"


def test_split_never_puts_one_session_on_both_sides(db_path, tmp_path):
    for turn in range(6):
        _add(db_path, "one-session", index=turn)
    for session in range(40):
        _add(db_path, f"s{session}", user=f"distinct prompt number {session}")
    curate(db_path, CurationPolicy(keep_per_cluster=99))
    export_dataset(db_path, tmp_path / "out", ExportOptions(eval_fraction=0.5))

    train = {e["meta"]["session_id"] for e in _read(tmp_path / "out" / "train.jsonl")}
    evals = {e["meta"]["session_id"] for e in _read(tmp_path / "out" / "eval.jsonl")}
    assert train and evals
    assert not (train & evals)


def test_split_is_deterministic_across_runs(db_path, tmp_path):
    for session in range(30):
        _add(db_path, f"s{session}", user=f"distinct prompt number {session}")
    curate(db_path, CurationPolicy(keep_per_cluster=99))

    export_dataset(db_path, tmp_path / "a", ExportOptions(eval_fraction=0.3))
    export_dataset(db_path, tmp_path / "b", ExportOptions(eval_fraction=0.3))
    assert (tmp_path / "a" / "eval.jsonl").read_text() == (
        tmp_path / "b" / "eval.jsonl"
    ).read_text()


def test_export_can_filter_by_harness_and_reasoning(db_path, tmp_path):
    _add(db_path, "claude1", harness=HARNESS_CLAUDE_CODE)
    _add(
        db_path,
        "codex1",
        harness=HARNESS_CODEX,
        blocks=[
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}, "id": "t1"},
            {"type": "tool_result", "content": "a.txt", "tool_call_id": "t1"},
            {"type": "text", "text": "One file is present in that directory."},
        ],
    )
    curate(db_path)

    report = export_dataset(
        db_path,
        tmp_path / "codex",
        ExportOptions(harnesses=(HARNESS_CODEX,), eval_fraction=0.0),
    )
    assert report.train_examples == 1

    reasoning_only = export_dataset(
        db_path,
        tmp_path / "reasoning",
        ExportOptions(require_reasoning=True, eval_fraction=0.0),
    )
    assert reasoning_only.train_examples == 1
    assert _read(tmp_path / "reasoning" / "train.jsonl")[0]["meta"]["harness"] == (
        HARNESS_CLAUDE_CODE
    )


def test_system_prompt_is_redacted_only_when_redaction_is_on(db_path, tmp_path):
    _add(db_path, "s1", system_prompt="your token is ghp_SYSTEMbbbbCCCCddddEEEE")
    curate(db_path)

    export_dataset(db_path, tmp_path / "raw", ExportOptions(eval_fraction=0.0))
    assert "ghp_SYSTEMbbbbCCCCddddEEEE" in (tmp_path / "raw" / "train.jsonl").read_text()

    export_dataset(
        db_path, tmp_path / "clean", ExportOptions(eval_fraction=0.0, secrets=SECRETS_REDACT)
    )
    assert "ghp_SYSTEMbbbb" not in (tmp_path / "clean" / "train.jsonl").read_text()


def test_randomization_keeps_shape_without_keeping_the_value():
    """Neither the real token nor a placeholder — a same-shaped stand-in."""
    serialized = json.dumps(
        render_turn(
            "here is sk-ant-api03-ZZZZyyyyXXXXvvvv9999",
            _credential_blocks(),
            ExportOptions(secrets=SECRETS_RANDOMIZE),
        )
    )
    assert "ghp_AAAAbbbbCCCCddddEEEEffff" not in serialized
    assert "sk-ant-api03-ZZZZyyyyXXXXvvvv9999" not in serialized
    assert "REDACTED" not in serialized
    assert "ghp_" in serialized
    assert "sk-ant-api03-" in serialized


def test_invalid_options_are_rejected():
    with pytest.raises(ValueError):
        ExportOptions(mode="hallucinate")
    with pytest.raises(ValueError):
        ExportOptions(secrets="yolo")
    with pytest.raises(ValueError):
        ExportOptions(eval_fraction=1.5)
