"""Unit tests for the training pipeline CLI.

The CLI is the seam the console calls across a container boundary, so its
contract is narrow and worth pinning: one JSON object on stdout for every
subcommand, a non-zero exit and a JSON error object on failure, and never a
traceback leaking to stderr.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from core.training_capture import HARNESS_CLAUDE_CODE, TrainingTurn, init_db, upsert_turn

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = REPO_ROOT / "tools" / "stateless" / "training_pipeline" / "training_pipeline.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("training_pipeline_cli", CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["training_pipeline_cli"] = module
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


@pytest.fixture
def corpus(tmp_path):
    path = tmp_path / "training_turns.db"
    init_db(path)
    blocks = [
        {"type": "thinking", "text": "look at the listing"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}, "id": "t1"},
        {"type": "tool_result", "content": "a.txt", "tool_call_id": "t1"},
        {"type": "text", "text": "One file is present in that directory."},
    ]
    for i in range(8):
        upsert_turn(
            path,
            TrainingTurn.from_blocks(
                session_id=f"s{i}",
                turn_index=0,
                harness=HARNESS_CLAUDE_CODE,
                user_content="run the recurring check",
                assistant_blocks=blocks,
                system_prompt="You are a mind.",
            ),
        )
    return path


@pytest.fixture
def ledger(tmp_path):
    return tmp_path / "training_runs.db"


def _run(capsys, argv):
    code = cli.main(argv)
    payload = json.loads(capsys.readouterr().out.strip())
    return code, payload


def _base(corpus, ledger):
    return ["--corpus", str(corpus), "--ledger", str(ledger)]


def test_status_reports_counts_before_any_curation(capsys, corpus, ledger):
    code, payload = _run(capsys, _base(corpus, ledger) + ["status"])
    assert code == 0
    assert payload["total"] == 8
    assert payload["by_flag"]["pending"] == 8
    assert payload["latest"]["curate"] is None


def test_status_on_a_missing_corpus_is_reported_not_raised(capsys, tmp_path, ledger):
    code, payload = _run(
        capsys,
        ["--corpus", str(tmp_path / "absent.db"), "--ledger", str(ledger), "status"],
    )
    assert code == 0
    assert payload["exists"] is False


def test_curate_collapses_duplicates_and_records_a_run(capsys, corpus, ledger):
    code, payload = _run(
        capsys, _base(corpus, ledger) + ["curate", "--keep-per-cluster", "2"]
    )
    assert code == 0
    assert payload["kept"] == 2
    assert payload["by_reason"]["near_duplicate"] == 6

    _, runs = _run(capsys, _base(corpus, ledger) + ["runs", "--kind", "curate"])
    assert runs["runs"][0]["status"] == "succeeded"
    assert runs["runs"][0]["report"]["kept"] == 2


def test_curate_reset_returns_rows_to_pending(capsys, corpus, ledger):
    _run(capsys, _base(corpus, ledger) + ["curate"])
    code, payload = _run(capsys, _base(corpus, ledger) + ["curate", "--reset"])
    assert code == 0
    assert payload["reset_rows"] == 8

    _, status = _run(capsys, _base(corpus, ledger) + ["status"])
    assert status["by_flag"]["pending"] == 8


def test_export_writes_jsonl_and_records_the_artifact(capsys, corpus, ledger, tmp_path):
    _run(capsys, _base(corpus, ledger) + ["curate", "--keep-per-cluster", "3"])
    out = tmp_path / "sets" / "v1"
    code, payload = _run(
        capsys,
        _base(corpus, ledger)
        + ["export", "--out-dir", str(out), "--eval-fraction", "0"],
    )
    assert code == 0
    assert payload["train_examples"] == 3
    assert (out / "train.jsonl").exists()

    lines = [json.loads(x) for x in (out / "train.jsonl").read_text().splitlines()]
    assert lines[0]["messages"][0]["role"] == "system"

    _, runs = _run(capsys, _base(corpus, ledger) + ["runs", "--kind", "export"])
    assert runs["runs"][0]["artifact_path"] == str(out)


def test_export_reasoning_mode_carries_thoughts(capsys, corpus, ledger, tmp_path):
    _run(capsys, _base(corpus, ledger) + ["curate"])
    out = tmp_path / "sets" / "reasoning"
    _run(
        capsys,
        _base(corpus, ledger)
        + ["export", "--mode", "reasoning", "--out-dir", str(out), "--eval-fraction", "0"],
    )
    body = (out / "train.jsonl").read_text()
    assert "reasoning" in body
    assert "look at the listing" in body


def test_train_dry_run_plans_without_launching(capsys, corpus, ledger, tmp_path):
    train_file = tmp_path / "train.jsonl"
    train_file.write_text('{"messages": []}\n')
    code, payload = _run(
        capsys,
        _base(corpus, ledger)
        + ["train", "--train-file", str(train_file), "--dry-run"],
    )
    assert code == 0
    assert "can_run" in payload
    assert "required_mib" in payload

    _, runs = _run(capsys, _base(corpus, ledger) + ["runs", "--kind", "train"])
    assert runs["runs"] == []


def test_failures_are_reported_as_json_with_a_nonzero_exit(capsys, tmp_path, ledger):
    code, payload = _run(
        capsys,
        ["--ledger", str(ledger), "train", "--train-file", str(tmp_path / "x"), "--dry-run"],
    )
    assert code == 0
    assert payload["can_run"] is False

    code, payload = _run(
        capsys,
        ["--corpus", str(tmp_path), "--ledger", str(ledger), "curate"],
    )
    assert code == 1
    assert "error" in payload


def test_every_subcommand_emits_exactly_one_json_object(capsys, corpus, ledger):
    for argv in (["status"], ["curate"], ["runs"]):
        cli.main(_base(corpus, ledger) + argv)
        out = capsys.readouterr().out.strip()
        assert len(out.splitlines()) == 1
        json.loads(out)
