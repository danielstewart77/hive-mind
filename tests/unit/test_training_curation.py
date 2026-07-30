"""Unit tests for the curation pass over the raw training corpus.

Covers each exclusion rule, near-duplicate cluster collapse (the dominant
defect in the real corpus, where scheduled skills fire hundreds of times),
idempotency, reset, and that curation never destroys captured content.
"""

from __future__ import annotations

import json

import pytest

from core.training_capture import (
    HARNESS_CLAUDE_CODE,
    HARNESS_CODEX,
    TrainingTurn,
    connect,
    init_db,
    upsert_turn,
)
from core.training_curation import (
    FLAG_EXCLUDED,
    FLAG_KEEP,
    FLAG_PENDING,
    REASON_DUPLICATE,
    REASON_HARNESS_ERROR,
    REASON_INTERRUPTED,
    REASON_MALFORMED,
    REASON_NO_RESPONSE,
    REASON_SECRET,
    REASON_TOO_LONG,
    REASON_TOO_SHORT,
    REASON_TOOL_ERRORS,
    CurationPolicy,
    cluster_key,
    corpus_stats,
    curate,
    normalize_for_cluster,
    reset_verdicts,
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "training_turns.db"
    init_db(path)
    return path


def _good_blocks(tool="Bash", command="ls"):
    return [
        {"type": "thinking", "text": "I should look at the directory listing first."},
        {"type": "tool_use", "name": tool, "input": {"command": command}, "id": "t1"},
        {"type": "tool_result", "content": "a.txt\nb.txt", "tool_call_id": "t1"},
        {"type": "text", "text": "Two files are present, a.txt and b.txt."},
    ]


def _add(db_path, session, index=0, blocks=None, user="list the files", **kwargs):
    turn = TrainingTurn.from_blocks(
        session_id=session,
        turn_index=index,
        harness=kwargs.pop("harness", HARNESS_CLAUDE_CODE),
        user_content=user,
        assistant_blocks=_good_blocks() if blocks is None else blocks,
        **kwargs,
    )
    upsert_turn(db_path, turn)
    return turn


def _flags(db_path):
    with connect(db_path) as conn:
        return {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT session_id || ':' || turn_index, quality_flag, "
                "exclusion_reason FROM training_turns"
            )
        }


def test_a_clean_turn_is_kept(db_path):
    _add(db_path, "s1")
    report = curate(db_path)
    assert report.kept == 1
    assert report.excluded == 0
    assert _flags(db_path)["s1:0"] == (FLAG_KEEP, None)


def test_malformed_blocks_are_excluded(db_path):
    _add(db_path, "s1")
    with connect(db_path) as conn:
        conn.execute("UPDATE training_turns SET assistant_blocks = ?", ("{not json",))
        conn.commit()
    curate(db_path)
    assert _flags(db_path)["s1:0"] == (FLAG_EXCLUDED, REASON_MALFORMED)


def test_empty_block_array_is_reported_as_no_response_not_malformed(db_path):
    """253 real rows land here; they parse fine and hold nothing."""
    _add(db_path, "s1")
    with connect(db_path) as conn:
        conn.execute("UPDATE training_turns SET assistant_blocks = ?", ("[]",))
        conn.commit()
    curate(db_path)
    assert _flags(db_path)["s1:0"] == (FLAG_EXCLUDED, REASON_NO_RESPONSE)


def test_interrupted_turn_is_excluded(db_path):
    _add(db_path, "s1", user="[Request interrupted by user]")
    curate(db_path)
    assert _flags(db_path)["s1:0"] == (FLAG_EXCLUDED, REASON_INTERRUPTED)


def test_turn_ending_on_an_unanswered_tool_call_is_excluded(db_path):
    blocks = [
        {"type": "text", "text": "let me check"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}, "id": "t9"},
    ]
    _add(db_path, "s1", blocks=blocks)
    curate(db_path)
    assert _flags(db_path)["s1:0"] == (FLAG_EXCLUDED, REASON_INTERRUPTED)


def test_harness_error_turn_is_excluded(db_path):
    blocks = [{"type": "text", "text": "API Error: Internal server error"}]
    _add(db_path, "s1", blocks=blocks)
    curate(db_path)
    assert _flags(db_path)["s1:0"] == (FLAG_EXCLUDED, REASON_HARNESS_ERROR)


def test_trivially_short_turn_without_tools_is_excluded(db_path):
    _add(db_path, "s1", blocks=[{"type": "text", "text": "ok"}], user="hi")
    curate(db_path)
    assert _flags(db_path)["s1:0"] == (FLAG_EXCLUDED, REASON_TOO_SHORT)


def test_oversized_turn_is_excluded(db_path):
    blocks = [
        {"type": "tool_use", "name": "Read", "input": {"path": "/big"}, "id": "t1"},
        {"type": "tool_result", "content": "x" * 500_000, "tool_call_id": "t1"},
        {"type": "text", "text": "that is a big file"},
    ]
    _add(db_path, "s1", blocks=blocks)
    curate(db_path, CurationPolicy(max_length_tokens=1_000))
    assert _flags(db_path)["s1:0"] == (FLAG_EXCLUDED, REASON_TOO_LONG)


def test_turn_dominated_by_failing_tools_is_excluded(db_path):
    blocks = [
        {"type": "tool_use", "name": "Bash", "input": {"command": "nope"}, "id": "t1"},
        {"type": "tool_result", "content": "bash: nope: command not found", "tool_call_id": "t1"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "nada"}, "id": "t2"},
        {"type": "tool_result", "content": "Permission denied", "tool_call_id": "t2"},
        {"type": "text", "text": "I could not run either command."},
    ]
    _add(db_path, "s1", blocks=blocks)
    curate(db_path)
    assert _flags(db_path)["s1:0"] == (FLAG_EXCLUDED, REASON_TOOL_ERRORS)


def test_a_single_failing_tool_call_that_recovers_is_kept(db_path):
    blocks = [
        {"type": "tool_use", "name": "Bash", "input": {"command": "nope"}, "id": "t1"},
        {"type": "tool_result", "content": "bash: nope: command not found", "tool_call_id": "t1"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}, "id": "t2"},
        {"type": "tool_result", "content": "a.txt", "tool_call_id": "t2"},
        {"type": "tool_use", "name": "Read", "input": {"path": "a.txt"}, "id": "t3"},
        {"type": "tool_result", "content": "contents", "tool_call_id": "t3"},
        {"type": "text", "text": "Recovered and read the file."},
    ]
    _add(db_path, "s1", blocks=blocks)
    curate(db_path)
    assert _flags(db_path)["s1:0"][0] == FLAG_KEEP


def test_secret_bearing_rows_are_flagged_but_kept_by_default(db_path):
    blocks = _good_blocks()
    blocks[2]["content"] = "GITHUB_TOKEN=ghp_AAAAbbbbCCCCddddEEEEffff"
    _add(db_path, "s1", blocks=blocks)
    report = curate(db_path)
    assert report.secret_rows == 1
    assert _flags(db_path)["s1:0"][0] == FLAG_KEEP
    with connect(db_path) as conn:
        meta = json.loads(
            conn.execute("SELECT curation_meta FROM training_turns").fetchone()[0]
        )
    assert "github_token" in meta["secret_rules"]


def test_secret_bearing_rows_can_be_excluded_by_policy(db_path):
    blocks = _good_blocks()
    blocks[2]["content"] = "GITHUB_TOKEN=ghp_AAAAbbbbCCCCddddEEEEffff"
    _add(db_path, "s1", blocks=blocks)
    curate(db_path, CurationPolicy(exclude_secret_rows=True))
    assert _flags(db_path)["s1:0"] == (FLAG_EXCLUDED, REASON_SECRET)


def test_scheduled_skill_repeats_collapse_to_the_cluster_budget(db_path):
    """The real defect: one skill fired on a timer hundreds of times."""
    for i in range(50):
        _add(
            db_path,
            f"sched{i}",
            user=(
                "Base directory for this skill: /skills/respond-to-netsage-alert\n"
                f"Alert at 2026-07-{i % 28 + 1:02d}T04:00:00 id "
                "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            ),
        )
    report = curate(db_path, CurationPolicy(keep_per_cluster=3))
    assert report.kept == 3
    assert report.by_reason[REASON_DUPLICATE] == 47
    assert report.clusters == 1


def test_different_tool_shapes_are_different_clusters(db_path):
    """Same prompt, genuinely different behaviour, both survive."""
    for i in range(10):
        _add(db_path, f"a{i}", user="run the check", blocks=_good_blocks(tool="Bash"))
    for i in range(10):
        _add(db_path, f"b{i}", user="run the check", blocks=_good_blocks(tool="Grep"))
    report = curate(db_path, CurationPolicy(keep_per_cluster=2))
    assert report.clusters == 2
    assert report.kept == 4


def test_cluster_collapse_prefers_reasoning_then_breadth(db_path):
    plain = [
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}, "id": "t1"},
        {"type": "tool_result", "content": "a.txt", "tool_call_id": "t1"},
        {"type": "text", "text": "There is one file present in that directory."},
    ]
    for i in range(5):
        _add(db_path, f"plain{i}", user="run the check", blocks=plain)
    _add(db_path, "rich", user="run the check", blocks=_good_blocks())

    curate(db_path, CurationPolicy(keep_per_cluster=1))
    kept = [k for k, (flag, _) in _flags(db_path).items() if flag == FLAG_KEEP]
    assert kept == ["rich:0"]


def test_curation_is_idempotent(db_path):
    for i in range(10):
        _add(db_path, f"s{i}", user="same prompt every time")
    first = curate(db_path, CurationPolicy(keep_per_cluster=2))
    second = curate(db_path, CurationPolicy(keep_per_cluster=2))
    assert first.kept == second.kept == 2
    assert first.by_reason == second.by_reason


def test_curation_never_deletes_or_alters_captured_content(db_path):
    _add(db_path, "s1")
    with connect(db_path) as conn:
        before = conn.execute(
            "SELECT user_content, assistant_blocks FROM training_turns"
        ).fetchone()
    curate(db_path)
    with connect(db_path) as conn:
        after = conn.execute(
            "SELECT user_content, assistant_blocks FROM training_turns"
        ).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM training_turns").fetchone()[0]
    assert before == after
    assert count == 1


def test_reset_returns_every_row_to_pending(db_path):
    for i in range(5):
        _add(db_path, f"s{i}", user="same prompt")
    curate(db_path, CurationPolicy(keep_per_cluster=1))
    assert reset_verdicts(db_path) == 5
    assert {flag for flag, _ in _flags(db_path).values()} == {FLAG_PENDING}


def test_policy_can_restrict_to_one_harness(db_path):
    _add(db_path, "c1", harness=HARNESS_CLAUDE_CODE)
    _add(db_path, "x1", harness=HARNESS_CODEX)
    report = curate(db_path, CurationPolicy(harnesses=(HARNESS_CODEX,)))
    assert report.total == 1


def test_normalize_collapses_timestamps_and_ids():
    a = normalize_for_cluster("run at 2026-07-30T04:00:00 id 12 times", 500)
    b = normalize_for_cluster("run at 2026-07-29T09:13:44 id 87 times", 500)
    assert a == b


def test_cluster_key_is_stable_and_shape_sensitive():
    blocks = _good_blocks()
    assert cluster_key("p", blocks, 500) == cluster_key("p", blocks, 500)
    assert cluster_key("p", blocks, 500) != cluster_key(
        "p", _good_blocks(tool="Grep"), 500
    )


def test_corpus_stats_reports_counts_without_content(db_path):
    _add(db_path, "s1")
    _add(db_path, "s2", user="[Request interrupted by user]")
    curate(db_path)
    stats = corpus_stats(db_path)
    assert stats["total"] == 2
    assert stats["sessions"] == 2
    assert stats["by_flag"][FLAG_KEEP] == 1
    assert stats["by_exclusion_reason"][REASON_INTERRUPTED] == 1
    assert "user_content" not in json.dumps(stats)


def test_policy_fingerprint_changes_with_policy():
    assert CurationPolicy().fingerprint() == CurationPolicy().fingerprint()
    assert CurationPolicy().fingerprint() != CurationPolicy(keep_per_cluster=9).fingerprint()
