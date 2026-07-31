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
    # One turn carrying an invented credential, so the redaction flag has
    # something real to act on. Never paste a value out of the live corpus.
    upsert_turn(
        path,
        TrainingTurn.from_blocks(
            session_id="creds",
            turn_index=0,
            harness=HARNESS_CLAUDE_CODE,
            user_content="show me the environment file for the deploy",
            assistant_blocks=[
                {"type": "thinking", "text": "read the env file"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "cat .env"}, "id": "c1"},
                {
                    "type": "tool_result",
                    "content": "GITHUB_TOKEN=ghp_INVENTEDfixture000000000000",
                    "tool_call_id": "c1",
                },
                {"type": "text", "text": "That file holds the deploy token."},
            ],
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
    assert payload["total"] == 9
    assert payload["by_flag"]["pending"] == 9
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
    assert payload["kept"] == 3
    assert payload["by_reason"]["near_duplicate"] == 6

    _, runs = _run(capsys, _base(corpus, ledger) + ["runs", "--kind", "curate"])
    assert runs["runs"][0]["status"] == "succeeded"
    assert runs["runs"][0]["report"]["kept"] == 3


def test_curate_reset_returns_rows_to_pending(capsys, corpus, ledger):
    _run(capsys, _base(corpus, ledger) + ["curate"])
    code, payload = _run(capsys, _base(corpus, ledger) + ["curate", "--reset"])
    assert code == 0
    assert payload["reset_rows"] == 9

    _, status = _run(capsys, _base(corpus, ledger) + ["status"])
    assert status["by_flag"]["pending"] == 9


def test_export_writes_jsonl_and_records_the_artifact(capsys, corpus, ledger, tmp_path):
    _run(capsys, _base(corpus, ledger) + ["curate", "--keep-per-cluster", "3"])
    out = tmp_path / "sets" / "v1"
    code, payload = _run(
        capsys,
        _base(corpus, ledger)
        + ["export", "--out-dir", str(out), "--eval-fraction", "0"],
    )
    assert code == 0
    assert payload["train_examples"] == 4
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


def test_export_randomizes_credentials_by_default(capsys, corpus, ledger, tmp_path):
    _run(capsys, _base(corpus, ledger) + ["curate"])
    out = tmp_path / "default"
    _run(
        capsys,
        _base(corpus, ledger) + ["export", "--out-dir", str(out), "--eval-fraction", "0"],
    )
    body = (out / "train.jsonl").read_text()
    assert "ghp_INVENTEDfixture000000000000" not in body
    assert "REDACTED" not in body
    assert "ghp_" in body


def test_export_keeps_credentials_only_when_asked(capsys, corpus, ledger, tmp_path):
    _run(capsys, _base(corpus, ledger) + ["curate"])
    raw = tmp_path / "raw"
    _run(
        capsys,
        _base(corpus, ledger)
        + [
            "export",
            "--out-dir",
            str(raw),
            "--eval-fraction",
            "0",
            "--secrets",
            "keep",
        ],
    )
    body = (raw / "train.jsonl").read_text()
    assert "ghp_INVENTEDfixture000000000000" in body
    assert "REDACTED" not in body


def test_export_randomize_keeps_shape_and_drops_the_value(
    capsys, corpus, ledger, tmp_path
):
    _run(capsys, _base(corpus, ledger) + ["curate"])
    out = tmp_path / "random"
    _run(
        capsys,
        _base(corpus, ledger)
        + [
            "export",
            "--out-dir",
            str(out),
            "--eval-fraction",
            "0",
            "--secrets",
            "randomize",
        ],
    )
    body = (out / "train.jsonl").read_text()
    assert "ghp_INVENTEDfixture000000000000" not in body
    assert "REDACTED" not in body
    assert "ghp_" in body


def test_export_redact_writes_placeholders(capsys, corpus, ledger, tmp_path):
    _run(capsys, _base(corpus, ledger) + ["curate"])
    out = tmp_path / "clean"
    _run(
        capsys,
        _base(corpus, ledger)
        + ["export", "--out-dir", str(out), "--eval-fraction", "0", "--secrets", "redact"],
    )
    body = (out / "train.jsonl").read_text()
    assert "ghp_INVENTEDfixture" not in body
    assert "REDACTED" in body


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


def test_base_models_lists_the_catalog_with_a_default(capsys, corpus, ledger):
    code, payload = _run(capsys, _base(corpus, ledger) + ["base-models"])
    assert code == 0
    ids = [model["id"] for model in payload["models"]]
    assert payload["default"] in ids
    assert all(model["note"] for model in payload["models"])


def test_train_passes_every_knob_into_the_spec(capsys, corpus, ledger, tmp_path):
    """The console exposes these; a knob that never reaches the spec is a lie."""
    train_file = tmp_path / "train.jsonl"
    train_file.write_text('{"messages": []}\n')
    code, payload = _run(
        capsys,
        _base(corpus, ledger)
        + [
            "train",
            "--train-file",
            str(train_file),
            "--base-model",
            "Qwen/Qwen3-14B",
            "--lora-rank",
            "16",
            "--lora-dropout",
            "0.1",
            "--epochs",
            "3",
            "--batch-size",
            "2",
            "--gradient-accumulation-steps",
            "8",
            "--max-sequence-length",
            "4096",
            "--warmup-ratio",
            "0.1",
            "--no-4bit",
            "--seed",
            "5",
            "--dry-run",
        ],
    )
    assert code == 0
    spec = payload["spec"]
    assert spec["base_model"] == "Qwen/Qwen3-14B"
    assert spec["lora_rank"] == 16
    assert spec["lora_alpha"] == 32, "alpha defaults to twice the rank"
    assert spec["lora_dropout"] == 0.1
    assert spec["epochs"] == 3
    assert spec["per_device_batch_size"] == 2
    assert spec["gradient_accumulation_steps"] == 8
    assert spec["max_sequence_length"] == 4096
    assert spec["warmup_ratio"] == 0.1
    assert spec["load_in_4bit"] is False
    assert spec["seed"] == 5


def test_an_explicit_lora_alpha_overrides_the_derived_one(
    capsys, corpus, ledger, tmp_path
):
    train_file = tmp_path / "train.jsonl"
    train_file.write_text('{"messages": []}\n')
    _, payload = _run(
        capsys,
        _base(corpus, ledger)
        + [
            "train",
            "--train-file",
            str(train_file),
            "--lora-rank",
            "16",
            "--lora-alpha",
            "128",
            "--dry-run",
        ],
    )
    assert payload["spec"]["lora_alpha"] == 128


def test_deploy_records_a_refusal_in_the_ledger(capsys, corpus, ledger, tmp_path):
    """A deploy that never reached Ollama still has to be visible as history."""
    code, payload = _run(
        capsys,
        _base(corpus, ledger)
        + [
            "deploy",
            "--name",
            "skippy-lora",
            "--adapter-dir",
            str(tmp_path / "missing"),
            "--base-model",
            "Qwen/Qwen3-8B",
        ],
    )
    assert code == 0
    assert payload["deployed"] is False
    assert payload["stage"] == "refused"

    _, runs = _run(capsys, _base(corpus, ledger) + ["runs", "--kind", "deploy"])
    assert runs["runs"][0]["status"] == "failed"


def test_deploy_takes_the_serve_tag_from_the_catalog(capsys, corpus, ledger, tmp_path):
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / "adapter_model.safetensors").write_bytes(b"weights")
    captured = {}

    def fake_deploy(request, ollama_url=None, trainer_image=None):
        captured["serve_tag"] = request.serve_tag
        from core.training_deploy import DeployResult

        return DeployResult(
            deployed=True, model_name=request.model_name, stage="served"
        )

    original = cli.deploy
    cli.deploy = fake_deploy
    try:
        code, payload = _run(
            capsys,
            _base(corpus, ledger)
            + [
                "deploy",
                "--name",
                "skippy-lora",
                "--adapter-dir",
                str(adapter),
                "--base-model",
                "Qwen/Qwen3-8B",
            ],
        )
    finally:
        cli.deploy = original

    assert code == 0
    assert captured["serve_tag"] == "qwen3:8b"
    assert payload["deployed"] is True

    _, runs = _run(capsys, _base(corpus, ledger) + ["runs", "--kind", "deploy"])
    assert runs["runs"][0]["status"] == "succeeded"


def test_status_lists_exported_datasets_and_trained_adapters(
    capsys, corpus, ledger, tmp_path, monkeypatch
):
    """The train and deploy forms ask for paths; status is where they come from."""
    sets_dir = tmp_path / "training_sets"
    run_dir = sets_dir / "v1"
    (run_dir / "adapter").mkdir(parents=True)
    (run_dir / "train.jsonl").write_text('{"messages": []}\n{"messages": []}\n')
    (run_dir / "eval.jsonl").write_text('{"messages": []}\n')
    (run_dir / "finetune_spec.json").write_text(
        json.dumps({"output_name": "skippy-lora", "base_model": "Qwen/Qwen3-8B"})
    )
    (run_dir / "adapter" / "adapter_model.safetensors").write_bytes(b"weights")
    monkeypatch.setattr(cli, "DEFAULT_SETS_DIR", sets_dir)

    _, payload = _run(capsys, _base(corpus, ledger) + ["status"])

    dataset = payload["datasets"][0]
    assert dataset["name"] == "v1"
    assert dataset["train_examples"] == 2
    assert dataset["eval_examples"] == 1

    adapter = payload["adapters"][0]
    assert adapter["name"] == "skippy-lora"
    assert adapter["trained"] is True
    assert adapter["base_model"] == "Qwen/Qwen3-8B"
