"""Unit tests for fine-tune job planning.

The launcher itself needs Docker and a GPU, so these tests exercise the pure
decision layer: spec validation, VRAM feasibility, and that a blocked launch
is a structured refusal rather than an exception.
"""

from __future__ import annotations

import json

import pytest

from core.training_finetune import (
    FineTuneSpec,
    GpuState,
    estimate_required_mib,
    launch,
    ollama_modelfile,
    plan_run,
    write_spec,
)


@pytest.fixture
def train_file(tmp_path):
    path = tmp_path / "train.jsonl"
    path.write_text('{"messages": []}\n')
    return path


def _spec(train_file, **kwargs):
    return FineTuneSpec(train_file=str(train_file), **kwargs)


def test_a_valid_spec_reports_no_problems(train_file):
    assert _spec(train_file).validate() == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"epochs": 0},
        {"learning_rate": 0},
        {"learning_rate": 5},
        {"lora_rank": 0},
        {"max_sequence_length": 128},
    ],
)
def test_invalid_hyperparameters_are_reported(train_file, kwargs):
    assert _spec(train_file, **kwargs).validate()


def test_missing_train_file_is_reported(tmp_path):
    spec = FineTuneSpec(train_file=str(tmp_path / "absent.jsonl"))
    assert any("not found" in problem for problem in spec.validate())


def test_effective_batch_size_multiplies_accumulation(train_file):
    spec = _spec(train_file, per_device_batch_size=2, gradient_accumulation_steps=8)
    assert spec.effective_batch_size() == 16


def test_four_bit_needs_less_vram_than_full_precision(train_file):
    quantized = estimate_required_mib(_spec(train_file, load_in_4bit=True))
    full = estimate_required_mib(_spec(train_file, load_in_4bit=False))
    assert quantized < full


def test_longer_sequences_need_more_vram(train_file):
    short = estimate_required_mib(_spec(train_file, max_sequence_length=2_048))
    long = estimate_required_mib(_spec(train_file, max_sequence_length=16_384))
    assert long > short


def test_plan_allows_a_run_when_the_gpu_is_free(train_file, monkeypatch):
    monkeypatch.setattr("core.training_finetune.shutil.which", lambda _: "/usr/bin/docker")
    gpu = GpuState(name="A6000", total_mib=49_140, used_mib=1_000, available=True)
    plan = plan_run(_spec(train_file), gpu=gpu)
    assert plan.can_run
    assert plan.blockers == []


def test_plan_blocks_a_run_when_the_gpu_is_busy(train_file, monkeypatch):
    """The live case: inference is holding most of the card."""
    monkeypatch.setattr("core.training_finetune.shutil.which", lambda _: "/usr/bin/docker")
    gpu = GpuState(name="A6000", total_mib=49_140, used_mib=48_000, available=True)
    plan = plan_run(_spec(train_file), gpu=gpu)
    assert not plan.can_run
    assert any("MiB free" in b for b in plan.blockers)


def test_plan_blocks_when_no_gpu_is_visible(train_file, monkeypatch):
    monkeypatch.setattr("core.training_finetune.shutil.which", lambda _: "/usr/bin/docker")
    plan = plan_run(_spec(train_file), gpu=GpuState())
    assert not plan.can_run
    assert any("no GPU" in b for b in plan.blockers)


def test_plan_blocks_when_docker_is_absent(train_file, monkeypatch):
    monkeypatch.setattr("core.training_finetune.shutil.which", lambda _: None)
    gpu = GpuState(name="A6000", total_mib=49_140, used_mib=0, available=True)
    plan = plan_run(_spec(train_file), gpu=gpu)
    assert not plan.can_run
    assert any("docker" in b for b in plan.blockers)


def test_blocked_launch_refuses_structurally_and_still_writes_the_spec(
    train_file, tmp_path, monkeypatch
):
    monkeypatch.setattr("core.training_finetune.shutil.which", lambda _: "/usr/bin/docker")
    gpu = GpuState(name="A6000", total_mib=49_140, used_mib=49_000, available=True)
    result = launch(_spec(train_file), tmp_path / "out", gpu=gpu)
    assert result["launched"] is False
    assert result["plan"]["blockers"]
    assert (tmp_path / "out" / "finetune_spec.json").exists()


def test_write_spec_round_trips(train_file, tmp_path):
    spec = _spec(train_file, epochs=3, lora_rank=64)
    path = write_spec(spec, tmp_path)
    loaded = json.loads(path.read_text())
    assert loaded["epochs"] == 3
    assert loaded["lora_rank"] == 64


def test_modelfile_serves_the_adapter_over_a_shared_base(train_file):
    text = ollama_modelfile(_spec(train_file), "/adapters/v1", "qwen3:30b")
    assert text.splitlines()[0] == "FROM qwen3:30b"
    assert "ADAPTER /adapters/v1" in text
