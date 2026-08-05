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
import textwrap
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
    assert trainer.build_sft_kwargs({}, Path("/out"), has_eval=True)["logging_steps"] == 1


def test_gradient_checkpointing_passes_use_reentrant_explicitly(trainer):
    """Requirement: the run does not depend on a workaround to learn.

    Reentrant checkpointing drops gradients when nothing entering a
    checkpointed block requires grad, which is what a frozen 4-bit base
    under LoRA looks like — and torch 2.9 turns the unset default into
    an error rather than a warning.
    """
    kwargs = trainer.build_sft_kwargs({}, Path("/out"), has_eval=True)
    assert kwargs["gradient_checkpointing"] is True
    assert kwargs["gradient_checkpointing_kwargs"] == {"use_reentrant": False}


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


# ------------------------------------------------- the evaluation OOM


def test_evaluation_uses_the_training_batch_size_unless_the_spec_names_one(
    trainer, tmp_path
):
    """Requirement 1: eval processes as many examples at a time as training.

    Transformers defaults ``per_device_eval_batch_size`` to 8 while this
    spec asks for a train batch of 1. Eight sequences of 8192 tokens
    against a 151936-token vocabulary is nineteen gigabytes of logits,
    which is exactly the allocation that killed the 2026-08-04 run at the
    first epoch boundary — after a full healthy epoch, and after the
    control plane had signed off on a card sized for a batch of one.
    """
    # A train batch above the default is the only case that separates
    # "mirrors training" from "hard-coded to one".
    mirrored = trainer.build_sft_kwargs(
        {"per_device_batch_size": 4}, tmp_path, has_eval=True
    )
    assert mirrored["per_device_train_batch_size"] == 4
    assert mirrored["per_device_eval_batch_size"] == 4

    named = trainer.build_sft_kwargs(
        {"per_device_batch_size": 4, "per_device_eval_batch_size": 1},
        tmp_path,
        has_eval=True,
    )
    assert named["per_device_eval_batch_size"] == 1

    # A dict nothing consumes satisfies every assertion above while the
    # run still builds its config inline, so the wiring is part of the
    # requirement. trl cannot be imported outside the trainer image.
    source = Path(trainer.__file__).read_text()
    assert "SFTConfig(**build_sft_kwargs(" in source


def test_the_end_of_an_epoch_writes_an_epoch_stamped_adapter(trainer, tmp_path):
    """Requirement 3: an epoch's adapter is on disk before its evaluation.

    Transformers fires ``on_epoch_end`` (trainer.py:2789) before
    ``_maybe_log_save_evaluate`` (2790), inside which evaluation (3221)
    precedes the checkpoint save (3228). So the epoch save has to hang off
    ``on_epoch_end`` — the built-in ``save_strategy="epoch"`` is on the
    wrong side of the evaluation that crashes.

    The directory is stamped with the epoch and is not ``out_dir`` itself:
    ``_adapters()`` reads a bare ``adapter_model.safetensors`` as "this run
    finished", and writing one there at every epoch would offer a
    half-trained adapter to deploy as though the run had completed.
    """
    saved = []

    class _Model:
        def save_pretrained(self, path):
            Path(path).mkdir(parents=True, exist_ok=True)
            (Path(path) / "adapter_model.safetensors").write_bytes(b"weights")
            saved.append(path)

    written = trainer.save_epoch_adapter(_Model(), tmp_path, epoch=1.0)

    assert written == tmp_path / "epoch-1"
    assert (tmp_path / "epoch-1" / "adapter_model.safetensors").is_file()
    # Not where the pipeline looks for a finished run.
    assert not (tmp_path / "adapter_model.safetensors").exists()

    # Every epoch, each under its own number — a constant stamp would have
    # epoch two overwrite epoch one.
    assert trainer.save_epoch_adapter(_Model(), tmp_path, epoch=2.0) == (
        tmp_path / "epoch-2"
    )
    # An epoch cut short mid-way rounds up to the epoch it was inside.
    assert trainer.epoch_adapter_dir(tmp_path, 1.4) == tmp_path / "epoch-2"

    # Staged and renamed, so a kill mid-write cannot leave a truncated
    # weights file that every downstream reader accepts as an adapter.
    class _DyingModel:
        def save_pretrained(self, path):
            Path(path).mkdir(parents=True, exist_ok=True)
            (Path(path) / "adapter_model.safetensors").write_bytes(b"trunc")
            raise OSError("killed mid-write")

    assert trainer.save_epoch_adapter(_DyingModel(), tmp_path, epoch=5.0) is None
    assert not (tmp_path / "epoch-5").exists()
    assert list(tmp_path.glob(".epoch-5*")) == []

    # Bound to the hook that fires before evaluation — and to no other, so
    # the save cannot drift onto on_evaluate or on_save and land on the far
    # side of the crash it exists to survive. The ordering itself lives in
    # the library and is verified against transformers 4.57.1.
    import ast
    import inspect

    factory = inspect.getsource(trainer._epoch_saver_class)
    tree = ast.parse(textwrap.dedent(factory))
    klass = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
    hooks = {n.name: n for n in klass.body if isinstance(n, ast.FunctionDef)}
    assert list(hooks) == ["on_epoch_end"]
    assert "save_epoch_adapter(" in ast.unparse(hooks["on_epoch_end"])

    # And it is actually attached to the trainer, unconditionally — a run
    # with no eval set still loses a completed epoch to a stopped container.
    source = Path(trainer.__file__).read_text()
    assert "trainer.add_callback(_epoch_saver_class(out_dir" in source
    assert "if has_eval:\n        trainer.add_callback(_epoch_saver_class" not in source


def test_a_failed_epoch_adapter_write_does_not_kill_the_run(trainer, tmp_path, capsys):
    """Requirement 4: the safety net cannot become the thing that drops you.

    ``CallbackHandler.call_event`` has no try/except, so an exception out
    of ``on_epoch_end`` propagates out of ``trainer.train()``. A full disk
    or a bind-mount blip during the epoch-1 write would then terminate a
    run that was going to finish — the write exists to save an hour of
    training, not to spend one.
    """

    class _RefusingModel:
        def save_pretrained(self, path):
            raise OSError(28, "No space left on device")

    assert trainer.save_epoch_adapter(_RefusingModel(), tmp_path, epoch=1.0) is None
    assert "epoch 1" in capsys.readouterr().out.lower()


def test_a_failed_run_records_the_newest_epoch_adapter_it_left_behind(
    trainer, tmp_path
):
    """Requirement 5: a lost run says what it left you.

    Nothing in the repo reads ``result.json``; the console lists adapters
    by scanning for weights files, and an epoch directory is deliberately
    somewhere it does not scan. Without the path in the failure record the
    recovered adapter exists and is invisible.
    """
    # Nine and ten, because "newest" sorted as text puts epoch-9 last.
    for epoch in (9, 10):
        (tmp_path / f"epoch-{epoch}").mkdir(parents=True)
        (tmp_path / f"epoch-{epoch}" / "adapter_model.safetensors").write_bytes(b"w")
    # An epoch that died mid-write has no weights and is not offered.
    (tmp_path / "epoch-11").mkdir()

    assert trainer.newest_epoch_adapter(tmp_path) == tmp_path / "epoch-10"
    assert trainer.newest_epoch_adapter(tmp_path / "empty") is None

    # A relaunch clears what the previous run left, so a recovered adapter
    # always belongs to the run whose failure named it.
    trainer.clear_epoch_adapters(tmp_path)
    assert trainer.newest_epoch_adapter(tmp_path) is None

    # And the failure record names it. run_training imports torch on its
    # first line, which is absent outside the trainer image — the same
    # shape as any other exception the run can die of.
    (tmp_path / "epoch-2").mkdir()
    (tmp_path / "epoch-2" / "adapter_model.safetensors").write_bytes(b"w")
    spec_path = tmp_path / "finetune_spec.json"
    spec_path.write_text(json.dumps({"output_dir": str(tmp_path)}))
    with pytest.raises(Exception):
        trainer.main(["--mode", "train", "--spec", str(spec_path)])

    record = json.loads((tmp_path / "result.json").read_text())
    assert record["status"] == "failed"
    assert record["recovered_adapter_dir"] == str(tmp_path / "epoch-2")
