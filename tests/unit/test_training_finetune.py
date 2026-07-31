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


def test_the_cache_directory_comes_from_the_environment(monkeypatch, tmp_path):
    """A container has to be told the host path; it cannot derive one."""
    from core.training_finetune import huggingface_cache_dir

    monkeypatch.setenv("HF_HOME", str(tmp_path / "models"))
    assert huggingface_cache_dir() == str(tmp_path / "models")


def test_an_uncreatable_cache_path_is_still_returned(monkeypatch):
    """The path is for the Docker daemon on the host, not for this process."""
    from pathlib import Path

    from core.training_finetune import huggingface_cache_dir

    monkeypatch.setenv("HF_HOME", "/home/daniel/.cache/huggingface")

    def refuse(*args, **kwargs):
        raise PermissionError("read-only container filesystem")

    monkeypatch.setattr(Path, "mkdir", refuse)
    assert huggingface_cache_dir() == "/home/daniel/.cache/huggingface"


def test_the_launch_command_mounts_the_cache_and_asks_for_train_mode(
    monkeypatch, train_file, tmp_path
):
    """Without the cache mount every run re-downloads the whole base model."""
    import subprocess

    from core.training_finetune import GpuState, launch

    monkeypatch.setenv("HF_HOME", str(tmp_path / "models"))
    captured = {}

    class _Completed:
        stdout = "abc123\n"

    def fake_run(command, **kwargs):
        captured["command"] = command
        return _Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)

    launch(
        _spec(train_file, max_sequence_length=1024),
        tmp_path,
        gpu=GpuState(total_mib=49_140, used_mib=0, name="A6000", available=True),
    )

    command = captured["command"]
    assert "--mode" in command and command[command.index("--mode") + 1] == "train"
    assert any(
        str(tmp_path / "models") in part and "/root/.cache/huggingface" in part
        for part in command
    )


def test_the_estimate_follows_the_base_model(train_file):
    """One constant for every model charged an 8B the footprint of a 30B."""
    from core.training_finetune import estimate_required_mib

    small = estimate_required_mib(_spec(train_file, base_model="Qwen/Qwen3-8B"))
    large = estimate_required_mib(
        _spec(train_file, base_model="Qwen/Qwen3-30B-A3B-Instruct-2507")
    )
    assert small < large


def test_an_eight_billion_parameter_run_fits_on_a_card_serving_inference(train_file):
    """The case that matters: an A6000 with half its VRAM already in use."""
    from core.training_finetune import GpuState, plan_run

    plan = plan_run(
        _spec(train_file, base_model="Qwen/Qwen3-8B", max_sequence_length=8_192),
        gpu=GpuState(total_mib=49_140, used_mib=24_500, name="A6000", available=True),
    )
    assert plan.can_run, plan.blockers


def test_an_unknown_base_is_charged_the_conservative_footprint(train_file):
    """A model the catalog has never heard of must not be waved through."""
    from core.training_finetune import UNKNOWN_MODEL_4BIT_MIB, estimate_required_mib

    estimate = estimate_required_mib(_spec(train_file, base_model="acme/mystery-70b"))
    assert estimate > UNKNOWN_MODEL_4BIT_MIB
