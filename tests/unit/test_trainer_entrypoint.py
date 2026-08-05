"""Unit tests for the trainer container's entrypoint.

torch is not installed outside the trainer image and never will be, so the
heavy path is untestable here by design — every import of it happens inside
a function for exactly that reason. What is testable is the part that has
actually broken things in practice: the container sees ``/workspace`` while
the spec carries host paths, and a run that dies has to leave a readable
record behind rather than only a container exit code.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

TRAINER_PATH = Path(__file__).resolve().parents[2] / "docker" / "trainer" / "train.py"


@pytest.fixture(scope="module")
def trainer():
    spec = importlib.util.spec_from_file_location("trainer_entrypoint", TRAINER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_an_existing_path_is_used_as_written(trainer, tmp_path):
    real = tmp_path / "train.jsonl"
    real.write_text("{}\n")
    assert trainer.resolve_in_workspace(str(real), tmp_path) == real


def test_a_host_path_is_remapped_into_the_workspace(trainer, tmp_path):
    """The launcher mounts the dataset dir; the spec still says /home/….

    The host path has to be one that cannot exist on the machine running
    the tests. Naming the real dataset directory made this pass only
    until the first export actually created it, at which point the test
    asserted the opposite of what it says.
    """
    (tmp_path / "train.jsonl").write_text("{}\n")
    resolved = trainer.resolve_in_workspace(
        str(tmp_path / "no-such-host-dir" / "training_sets" / "v1" / "train.jsonl"),
        tmp_path,
    )
    assert resolved == tmp_path / "train.jsonl"


def test_a_nested_host_path_is_found_one_level_down(trainer, tmp_path):
    nested = tmp_path / "adapter"
    nested.mkdir()
    (nested / "adapter_model.safetensors").write_bytes(b"x")
    resolved = trainer.resolve_in_workspace(
        "/host/somewhere/adapter/adapter_model.safetensors", tmp_path
    )
    assert resolved == nested / "adapter_model.safetensors"


def test_an_unresolvable_path_names_the_workspace_location(trainer, tmp_path):
    """So the error says where it looked, not where the host would have it."""
    resolved = trainer.resolve_in_workspace("/host/gone/train.jsonl", tmp_path)
    assert resolved == tmp_path / "train.jsonl"


def test_an_unwritable_output_path_is_remapped(trainer, tmp_path):
    resolved = trainer.output_dir_in_workspace("/host/never/exists/adapter", tmp_path)
    assert resolved == tmp_path / "adapter"


def test_a_writable_output_path_is_kept(trainer, tmp_path):
    resolved = trainer.output_dir_in_workspace(str(tmp_path / "adapter"), tmp_path)
    assert resolved == tmp_path / "adapter"


def test_jsonl_loading_skips_blank_lines(trainer, tmp_path):
    path = tmp_path / "train.jsonl"
    path.write_text('{"messages": [{"role": "user"}]}\n\n{"messages": []}\n')
    assert len(trainer.load_jsonl(path)) == 2


def test_a_result_file_records_the_outcome(trainer, tmp_path):
    trainer.write_result(tmp_path / "adapter", {"status": "succeeded", "mode": "train"})
    written = json.loads((tmp_path / "adapter" / "result.json").read_text())
    assert written["status"] == "succeeded"


class _TemplateTokenizer:
    def apply_chat_template(self, messages, tokenize, add_generation_prompt):
        return "|".join(m["role"] for m in messages)


class _RefusingTokenizer:
    def apply_chat_template(self, *args, **kwargs):
        raise ValueError("this template does not support tool calls")


def test_rendering_uses_the_models_own_chat_template(trainer):
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    assert trainer.render_example(messages, _TemplateTokenizer()) == "user|assistant"


def test_a_template_that_rejects_tool_calls_degrades_instead_of_crashing(trainer):
    """Losing an example's fidelity beats losing the run three hours in."""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "ls"}}]}
    ]
    rendered = trainer.render_example(messages, _RefusingTokenizer())
    assert "assistant" in rendered
    assert "ls" in rendered


def test_the_parser_defaults_to_training(trainer):
    args = trainer.build_parser().parse_args([])
    assert args.mode == "train"


def test_merge_mode_takes_the_paths_it_needs(trainer):
    args = trainer.build_parser().parse_args(
        [
            "--mode",
            "merge",
            "--base-model",
            "Qwen/Qwen3-8B",
            "--adapter",
            "/workspace/adapter",
            "--out",
            "/workspace/merged.gguf",
            "--quantization",
            "q5_K_M",
        ]
    )
    assert args.mode == "merge"
    assert args.quantization == "q5_K_M"


def test_a_failed_training_run_writes_a_failure_record(trainer, tmp_path, monkeypatch):
    spec = {
        "train_file": str(tmp_path / "train.jsonl"),
        "output_dir": str(tmp_path / "adapter"),
        "base_model": "Qwen/Qwen3-8B",
    }
    spec_path = tmp_path / "finetune_spec.json"
    spec_path.write_text(json.dumps(spec))
    monkeypatch.setattr(trainer, "WORKSPACE", tmp_path)

    def boom(_spec):
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(trainer, "run_training", boom)

    with pytest.raises(RuntimeError):
        trainer.main(["--mode", "train", "--spec", str(spec_path)])

    written = json.loads((tmp_path / "adapter" / "result.json").read_text())
    assert written["status"] == "failed"
    assert "CUDA out of memory" in written["error"]


class _FakeCuda:
    def __init__(self, available=True, devices=1):
        self._available = available
        self._devices = devices
        self.applied = []

    def is_available(self):
        return self._available

    def device_count(self):
        return self._devices

    def set_per_process_memory_fraction(self, fraction, device):
        self.applied.append((fraction, device))


class _FakeTorch:
    def __init__(self, cuda):
        self.cuda = cuda


def test_the_memory_cap_is_applied_to_every_device(trainer):
    """Requirement 3: the reserve is enforced inside the training process.

    Planning arithmetic cannot hold VRAM back — the allocator grows to
    whatever is free, hours after anyone looked, and the display server on
    this card is what fails when it does.
    """
    cuda = _FakeCuda(devices=2)
    applied = trainer.apply_memory_cap({"gpu_memory_fraction": 0.958}, _FakeTorch(cuda))
    assert applied == 0.958
    assert cuda.applied == [(0.958, 0), (0.958, 1)]


def test_no_fraction_means_no_cap(trainer):
    """A headless machine, or an unreadable card, runs uncapped."""
    cuda = _FakeCuda()
    assert trainer.apply_memory_cap({}, _FakeTorch(cuda)) == 0.0
    assert trainer.apply_memory_cap({"gpu_memory_fraction": 0.0}, _FakeTorch(cuda)) == 0.0
    assert cuda.applied == []


def test_no_cuda_device_means_no_cap(trainer):
    cuda = _FakeCuda(available=False)
    assert trainer.apply_memory_cap({"gpu_memory_fraction": 0.9}, _FakeTorch(cuda)) == 0.0
    assert cuda.applied == []


def test_the_trainer_announces_each_stage_as_it_starts_it(trainer, capsys):
    """Requirement: something is on screen from launch until the run ends.

    Every expensive step here is silent by default, so without this the
    pane is empty for the quarter of an hour it takes to pull weights and
    a downloading run is indistinguishable from a hung one.
    """
    trainer.stage("loading base weights")
    captured = capsys.readouterr().out
    assert "loading base weights" in captured
    assert captured.endswith("\n")


def test_the_trainer_logs_every_step_not_every_tenth(trainer):
    """Requirement: as much feedback as the run can give.

    At an effective batch of sixteen an optimizer step is tens of
    seconds, so logging every tenth step is a five-minute silence in
    which a working run and a hung one are the same picture.
    """
    # The config is built inside run_training, which imports torch and trl
    # — neither of which exists outside the trainer image. The declared
    # value is what ships, so the declaration is what is checked.
    source = Path(trainer.__file__).read_text()
    assert "logging_steps=1," in source


def test_gradient_checkpointing_passes_use_reentrant_explicitly(trainer):
    """Requirement: the run does not depend on a workaround to learn.

    Reentrant checkpointing drops gradients when nothing entering a
    checkpointed block requires grad, which is what a frozen 4-bit base
    under LoRA looks like — and torch 2.9 turns the unset default into
    an error rather than a warning.
    """
    source = Path(trainer.__file__).read_text()
    assert 'gradient_checkpointing_kwargs={"use_reentrant": False}' in source


def test_the_progress_line_carries_position_pace_and_what_is_left(trainer):
    """Requirement: the console can say how much time is left.

    The estimate is made in the trainer because only the trainer knows
    when training began — the container's uptime includes the weight
    download, which on a cold cache is most of the first half hour.
    """
    line = trainer.format_progress(step=24, total=377, elapsed=758, loss=1.6377)
    assert "step 24/377" in line
    assert "6%" in line
    assert "elapsed 12m 38s" in line
    assert "eta 3h 05m" in line
    assert "loss 1.6377" in line

    # Before the first step there is no rate, and inventing one is worse
    # than admitting there isn't one.
    assert "eta unknown" in trainer.format_progress(step=0, total=377, elapsed=0)
