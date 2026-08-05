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


def test_a_refused_launch_writes_no_spec_file(train_file, tmp_path, monkeypatch):
    """Requirement 10: a refused launch cannot disturb a running trainer.

    The trainer reads its spec minutes after launch, after the image pull
    and the weights download. A refusal that wrote the spec anyway would
    hand a running job hyperparameters nobody selected.
    """
    monkeypatch.setattr("core.training_finetune.shutil.which", lambda _: "/usr/bin/docker")
    gpu = GpuState(name="A6000", total_mib=49_140, used_mib=49_000, available=True)
    out_dir = tmp_path / "out"
    result = launch(_spec(train_file), out_dir, gpu=gpu)
    assert result["launched"] is False
    assert result["plan"]["blockers"]
    assert not list(out_dir.glob("finetune_spec*.json"))


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


# --------------------------------------------------- GPU arbitration (R3, R5)


def test_the_desktop_reserve_is_held_back_on_top_of_current_usage(train_file):
    """Requirement 3: a job that would leave under the reserve is refused.

    The card also drives the display. The reserve is headroom for the
    compositor to *grow* into, so it sits on top of what the desktop
    already holds rather than being assumed to be part of it.
    """
    from core.training_finetune import DESKTOP_RESERVE_MIB

    spec = _spec(train_file)
    required = estimate_required_mib(spec)
    # Exactly enough for the job, one MiB short of also covering the reserve.
    total = 49_140
    used = total - (required + DESKTOP_RESERVE_MIB - 1)
    gpu = GpuState(name="A6000", total_mib=total, used_mib=used, available=True)
    assert not plan_run(spec, gpu=gpu).can_run

    roomier = GpuState(
        name="A6000",
        total_mib=total,
        used_mib=used - 1,
        available=True,
    )
    assert plan_run(spec, gpu=roomier).can_run


def test_the_memory_cap_leaves_the_reserve_unclaimable(train_file):
    """The fraction handed to the trainer is what physically enforces R3."""
    from core.training_finetune import DESKTOP_RESERVE_MIB, memory_fraction_for

    total = 49_140
    fraction = memory_fraction_for(total)
    assert 0 < fraction < 1
    claimable = total * fraction
    assert total - claimable >= DESKTOP_RESERVE_MIB - 1
    # An unreadable card means no cap rather than a refusal to run.
    assert memory_fraction_for(0) == 0.0


def test_launch_keeps_the_container_so_its_outcome_can_be_read(train_file, tmp_path, monkeypatch):
    """Requirement 5: restore must be able to name how the run ended.

    ``--rm`` deletes the container the instant it exits, leaving no exit
    code — a crash at hour three then reads identically to a success.
    """
    recorded = {}

    class _Completed:
        returncode = 0
        stdout = "abc123\n"
        stderr = ""

    def _fake_run(command, **kwargs):
        recorded["command"] = command
        return _Completed()

    monkeypatch.setattr("core.training_finetune.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr("core.training_finetune.subprocess.run", _fake_run)
    gpu = GpuState(name="A6000", total_mib=49_140, used_mib=0, available=True)
    result = launch(_spec(train_file), tmp_path / "out", gpu=gpu)

    assert result["launched"] is True
    assert "--rm" not in recorded["command"]


def test_the_job_spec_name_is_unique_per_job(train_file, tmp_path):
    """Two jobs sharing a dataset directory get their own spec files."""
    first = write_spec(_spec(train_file, output_name="run-a"), tmp_path)
    second = write_spec(_spec(train_file, output_name="run-b", epochs=9), tmp_path)
    assert first != second
    assert json.loads(first.read_text())["epochs"] != 9


def test_an_empty_training_file_is_not_a_usable_spec(tmp_path):
    """A curation pass that kept nothing must not read as a successful run."""
    empty = tmp_path / "train.jsonl"
    empty.write_text("")
    problems = FineTuneSpec(train_file=str(empty)).validate()
    assert any("empty" in problem for problem in problems)


def test_the_plan_counts_the_memory_a_launch_will_reclaim(train_file, monkeypatch):
    """The card is full of things the launch is about to stop.

    Requirement: feasibility judges against what will be free after
    preflight, names what has to die for it, and still refuses a job too
    big even with everything reclaimed.
    """
    monkeypatch.setattr("core.training_finetune.shutil.which", lambda _: "/usr/bin/docker")
    # Voice and Ollama between them hold all but ten gigabytes.
    gpu = GpuState(name="A6000", total_mib=49_140, used_mib=38_518, available=True)

    without_reclaim = plan_run(_spec(train_file), gpu=gpu)
    assert not without_reclaim.can_run

    fits = plan_run(_spec(train_file), gpu=gpu, reclaimable_mib=36_370)
    assert fits.can_run
    assert fits.reclaimable_mib == 36_370
    assert any("36370" in note for note in fits.notes)
    assert any("Ollama" in note for note in fits.notes)

    too_big = plan_run(
        _spec(train_file, max_sequence_length=131_072), gpu=gpu, reclaimable_mib=36_370
    )
    assert not too_big.can_run
    assert any("reclaimable" in b for b in too_big.blockers)
