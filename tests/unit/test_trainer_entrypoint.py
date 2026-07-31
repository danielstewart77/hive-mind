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
    """The launcher mounts the dataset dir; the spec still says /home/…."""
    (tmp_path / "train.jsonl").write_text("{}\n")
    resolved = trainer.resolve_in_workspace(
        "/home/daniel/Storage/Dev/hive_mind/data/training_sets/v1/train.jsonl",
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
