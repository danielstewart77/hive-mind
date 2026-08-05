#!/usr/bin/env python3
"""Trainer entrypoint: QLoRA fine-tune, or merge an adapter back into a base.

This is the only place in the hive that imports torch, and it runs in its
own container with the GPU attached. Everything upstream — planning,
launching, recording — happens in stdlib code that never has to install a
CUDA wheel.

Two modes, because both need the same heavyweight dependency set:

``train``  reads ``finetune_spec.json``, trains a LoRA adapter, writes it
           to the spec's output directory alongside a ``result.json`` the
           control plane polls for.
``merge``  folds a trained adapter into the base weights and converts the
           result to an f16 GGUF, which is what the deploy step needs and
           cannot do from a service image. It stops at f16: Ollama does the
           quantizing when it imports the file.

**Paths.** The control plane writes host paths into the spec, and this
container only sees the dataset directory, mounted at ``/workspace``. Rather
than have the launcher rewrite the spec — which would make the on-disk spec
a lie and un-rerunnable by hand — the spec keeps host paths and this script
remaps anything it cannot open to the same filename under ``/workspace``.
The spec stays the truth; the remap is a container detail.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

WORKSPACE = Path(os.environ.get("TRAINER_WORKSPACE", "/workspace"))
RESULT_NAME = "result.json"


class Terminated(Exception):
    """The run was cancelled from outside rather than failing on its own."""


def format_duration(seconds: float) -> str:
    """A duration a person reads at a glance: ``2h 14m`` or ``47s``."""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_progress(step: int, total: int, elapsed: float, loss: float | None = None) -> str:
    """One line carrying position, pace and what is left.

    The estimate is made here rather than by whoever reads the log,
    because only this process knows how long training has actually been
    running — a container's uptime includes the download, which on a
    cold cache is most of the first half hour and would put the first
    estimates out by an order of magnitude.
    """
    total = max(1, total)
    step = max(0, min(step, total))
    percent = int(round(100 * step / total))
    remaining = (elapsed / step) * (total - step) if step else 0
    line = (
        f"step {step}/{total} · {percent}% · elapsed {format_duration(elapsed)}"
        f" · eta {format_duration(remaining) if step else 'unknown'}"
    )
    if loss is not None:
        line += f" · loss {loss:.4f}"
    return line


def stage(message: str) -> None:
    """Announce what the run is about to spend minutes doing.

    Everything expensive here — importing torch, pulling tens of
    gigabytes of base weights, tokenizing a corpus — is silent by
    default, so a watcher sees an empty pane for a quarter of an hour and
    cannot tell a downloading run from a hung one. Each line is flushed
    on its own: buffered progress is the same as no progress to anyone
    reading ``docker logs``.
    """
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# ---------------------------------------------------------------- paths


def resolve_in_workspace(path: str, workspace: Path = WORKSPACE) -> Path:
    """The spec's path if it exists here, else the same name in /workspace.

    Falls back to the workspace-relative form even when nothing exists at
    either location, so the error the caller sees names the path it will
    actually look for.
    """
    candidate = Path(path)
    if candidate.exists():
        return candidate
    local = workspace / candidate.name
    if local.exists():
        return local
    nested = workspace / candidate.parent.name / candidate.name
    if nested.exists():
        return nested
    return local


def output_dir_in_workspace(path: str, workspace: Path = WORKSPACE) -> Path:
    """Where to write the adapter. Output never exists yet, so name-match."""
    candidate = Path(path)
    if candidate.parent.exists() and os.access(candidate.parent, os.W_OK):
        return candidate
    return workspace / candidate.name


# --------------------------------------------------------------- dataset


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def render_example(messages: list[dict], tokenizer) -> str:
    """One training example as text, using the model's own chat template.

    A tool-calling corpus is only worth training on if the tool calls are
    rendered the way the model will be asked to emit them at inference, so
    the tokenizer's template is the authority. Models whose template rejects
    ``tool_calls`` get a plain role-tagged rendering rather than a crash —
    a degraded example beats a dead run at hour three.
    """
    try:
        return tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=False
        )
    except Exception:  # noqa: BLE001 — template errors are model-specific
        parts = []
        for message in messages:
            role = message.get("role", "user")
            content = message.get("content") or ""
            calls = message.get("tool_calls")
            if calls:
                content = (content + "\n" if content else "") + json.dumps(calls)
            parts.append(f"<|{role}|>\n{content}")
        return "\n".join(parts)


def build_dataset(rows: list[dict], tokenizer):
    from datasets import Dataset

    texts = [
        render_example(row.get("messages") or [], tokenizer)
        for row in rows
        if row.get("messages")
    ]
    return Dataset.from_dict({"text": texts})


# ----------------------------------------------------------------- train


def write_result(out_dir: Path, payload: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / RESULT_NAME
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return path


def apply_memory_cap(spec: dict, torch) -> float:
    """Cap this process's share of the card, and return what was applied.

    The host's display server shares this GPU. Without a cap the caching
    allocator grows to whatever is free — typically at the first
    max-length batch, hours after anyone checked — and the next surface
    allocation Xorg makes fails, taking the desktop and every window on it
    down. A planning-time subtraction cannot prevent that; only a limit
    inside this process can.

    A fraction of zero or a machine with no CUDA device means no cap, so a
    headless run is unaffected.
    """
    fraction = float(spec.get("gpu_memory_fraction") or 0.0)
    if not 0.0 < fraction < 1.0:
        return 0.0
    if not torch.cuda.is_available():
        return 0.0
    for device in range(torch.cuda.device_count()):
        torch.cuda.set_per_process_memory_fraction(fraction, device)
    return fraction


def eval_batch_size(spec: dict) -> int:
    """How many examples evaluation sees at once.

    Transformers defaults this to 8 while a spec here asks for a train
    batch of 1. Eight sequences at 8192 tokens against Qwen3's 151936-token
    vocabulary is a nineteen-gigabyte logits tensor — the exact allocation
    that took down the run of 2026-08-04 at its first epoch boundary, after
    a full healthy epoch and on a card the control plane had sized for a
    batch of one.

    Absence means "match training", not zero: ``gpu_memory_fraction``
    already demonstrates what happens when a legitimate value and an
    unset field share a sentinel.
    """
    named = spec.get("per_device_eval_batch_size")
    if named is None:
        named = spec.get("per_device_batch_size", 1)
    size = int(named)
    if size < 1:
        # The container never runs the spec through FineTuneSpec.validate,
        # and the module docstring advertises hand-editing the file. A zero
        # reaches SFTConfig unchallenged and is rejected by the DataLoader
        # at the first epoch boundary, an hour in.
        raise ValueError(f"batch size must be at least 1, got {size}")
    return size


def clear_epoch_adapters(out_dir: Path) -> None:
    """Drop any epoch adapters from an earlier run of this output directory.

    The output directory is per dataset and nothing else empties it, so a
    relaunch that dies inside epoch 1 would otherwise "recover" an adapter
    from the previous run — different weights, possibly different
    hyperparameters, and the result.json naming it overwritten in place.
    """
    for entry in out_dir.glob("epoch-*"):
        shutil.rmtree(entry, ignore_errors=True)


def build_sft_kwargs(spec: dict, out_dir: Path, has_eval: bool) -> dict:
    """Every training argument, as data — so it is checkable without trl.

    trl and transformers exist only inside the trainer image, so a config
    assembled inline can be verified by nothing but a grep over this file,
    which cannot tell a batch of 1 from a batch of 8. A dict can be built
    and read anywhere.
    """
    return {
        "output_dir": str(out_dir / "checkpoints"),
        "num_train_epochs": spec.get("epochs", 2),
        "per_device_train_batch_size": spec.get("per_device_batch_size", 1),
        "per_device_eval_batch_size": eval_batch_size(spec),
        "gradient_accumulation_steps": spec.get("gradient_accumulation_steps", 16),
        "learning_rate": spec.get("learning_rate", 1e-4),
        "warmup_ratio": spec.get("warmup_ratio", 0.03),
        "max_length": spec.get("max_sequence_length", 8192),
        "bf16": True,
        # Every step, not every tenth. At an effective batch of sixteen a
        # step is tens of seconds, so ten of them is a five-minute silence
        # in which a working run and a hung one look the same.
        "logging_steps": 1,
        "save_strategy": "epoch",
        "eval_strategy": "epoch" if has_eval else "no",
        "gradient_checkpointing": True,
        # Reentrant checkpointing silently drops gradients when nothing
        # entering a checkpointed block requires grad — precisely the
        # shape of a frozen 4-bit base with LoRA adapters on top, which
        # works today only because Transformers applies an
        # input-requires-grad workaround on our behalf. Torch 2.9 is also
        # the release that turns the unset default into a hard error.
        "gradient_checkpointing_kwargs": {"use_reentrant": False},
        "report_to": [],
        "seed": spec.get("seed", 17),
        "dataset_text_field": "text",
        # A progress bar redraws one line with carriage returns, which is
        # a line that never ends and therefore never appears in `docker
        # logs`. Plain per-step lines are the only form a log reader sees.
        "disable_tqdm": True,
    }


def epoch_adapter_dir(out_dir: Path, epoch: float) -> Path:
    """Where a completed epoch's adapter lands.

    Deliberately not ``out_dir`` itself. The pipeline decides a run
    finished by finding ``adapter_model.safetensors`` directly under the
    output directory and reads nothing else — so writing one there every
    epoch would offer a half-trained adapter to deploy as though the run
    had completed, with the failure recorded in a file nothing opens.
    """
    return out_dir / f"epoch-{int(epoch) if epoch == int(epoch) else int(epoch) + 1}"


def save_epoch_adapter(model, out_dir: Path, epoch: float) -> Path | None:
    """Write a finished epoch's adapter. Returns the path, or None.

    Never raises. ``CallbackHandler.call_event`` has no try/except, so an
    exception here leaves ``trainer.train()`` entirely — and a full disk
    during the epoch-1 write would kill a run that was going to finish.
    This exists to save an hour of training, not to spend one.

    Staged and renamed because peft writes the weights first and the
    config last, straight to the target with no temp file: a process
    killed mid-write leaves a truncated ``adapter_model.safetensors``,
    which every reader downstream accepts as a usable adapter.
    """
    target = epoch_adapter_dir(out_dir, epoch)
    # Process-unique: two trainers sharing an output directory must not
    # delete each other's staging mid-write and each report success over a
    # truncated tree.
    staging = target.with_name(f".{target.name}.{os.getpid()}.partial")
    try:
        if staging.exists():
            shutil.rmtree(staging)
        model.save_pretrained(str(staging))
        if target.exists():
            shutil.rmtree(target)
        os.replace(staging, target)
    except Exception as exc:  # noqa: BLE001 — a lost safety net is not a lost run
        stage(f"could not save the adapter for epoch {int(epoch)}: {exc}")
        shutil.rmtree(staging, ignore_errors=True)
        return None
    stage(f"adapter for epoch {int(epoch)} saved to {target}")
    return target


def newest_epoch_adapter(out_dir: Path) -> Path | None:
    """The most recent epoch adapter left on disk, if any is complete.

    A directory without weights is an epoch that died mid-write, and
    offering it is worse than offering nothing.
    """
    candidates = []
    try:
        entries = list(out_dir.glob("epoch-*"))
    except OSError:
        return None
    for entry in entries:
        if (entry / "adapter_model.safetensors").is_file():
            try:
                candidates.append((int(entry.name.split("-")[-1]), entry))
            except ValueError:
                continue
    if not candidates:
        return None
    return max(candidates)[1]


def _epoch_saver_class(out_dir: Path, announce_eval: bool = False):
    """Built lazily: transformers only exists inside the trainer image.

    Bound to ``on_epoch_end``, which Transformers fires immediately before
    ``_maybe_log_save_evaluate`` — and inside that, evaluation runs before
    the checkpoint save. The built-in ``save_strategy="epoch"`` is
    therefore on the far side of the evaluation that crashed, which is why
    last night's run lost a completed epoch to an OOM and left an empty
    checkpoints directory behind.
    """
    from transformers import TrainerCallback

    class EpochSaver(TrainerCallback):
        def on_epoch_end(self, args, state, control, model=None, **kwargs):
            if model is not None:
                save_epoch_adapter(model, out_dir, float(state.epoch or 0))
            if announce_eval:
                # Evaluation logs nothing until it finishes, and at an eval
                # batch of one it is now minutes rather than seconds. An
                # unexplained pause on the same step is the picture
                # `logging_steps=1` exists to prevent.
                stage("evaluating — no step lines until this finishes")

    return EpochSaver


def _progress_reporter_class():
    """Built lazily: transformers only exists inside the trainer image."""
    from transformers import TrainerCallback

    class ProgressReporter(TrainerCallback):
        """Prints where the run is and when it will be done, every step."""

        def on_train_begin(self, args, state, control, **kwargs):
            self.started = time.time()

        def on_log(self, args, state, control, logs=None, **kwargs):
            loss = (logs or {}).get("loss")
            stage(
                format_progress(
                    int(state.global_step),
                    int(state.max_steps),
                    time.time() - getattr(self, "started", time.time()),
                    float(loss) if isinstance(loss, (int, float)) else None,
                )
            )

    return ProgressReporter


def run_training(spec: dict) -> dict:
    stage("loading torch and the training libraries")
    import torch
    from peft import LoraConfig
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from trl import SFTConfig, SFTTrainer

    capped = apply_memory_cap(spec, torch)
    stage(f"capped this process at {capped:.2f} of the card")

    train_file = resolve_in_workspace(spec["train_file"])
    if not train_file.exists():
        raise FileNotFoundError(f"training file not found: {train_file}")
    if train_file.stat().st_size == 0:
        raise ValueError(f"training file is empty: {train_file}")
    # An unset eval file must not become Path(""), which is the current
    # directory, which exists — so the run passes the existence check and
    # dies tokenizing a directory, after the weights download.
    declared_eval = str(spec.get("eval_file") or "").strip()
    eval_file = resolve_in_workspace(declared_eval) if declared_eval else None
    out_dir = output_dir_in_workspace(spec["output_dir"])
    # Before the weights download, not after: a bad batch size otherwise
    # surfaces at the first epoch boundary, an hour in.
    eval_batch_size(spec)
    clear_epoch_adapters(out_dir)

    stage(f"fetching the tokenizer for {spec['base_model']}")
    tokenizer = AutoTokenizer.from_pretrained(spec["base_model"], trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    quantization = None
    if spec.get("load_in_4bit", True):
        quantization = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
        )

    stage(
        f"loading base weights for {spec['base_model']} — this downloads tens of "
        "gigabytes the first time and is cached after"
    )
    model = AutoModelForCausalLM.from_pretrained(
        spec["base_model"],
        quantization_config=quantization,
        dtype=torch.bfloat16,
        device_map="auto",
        trust_remote_code=True,
    )
    model.config.use_cache = False
    stage("base weights are on the card")

    peft_config = LoraConfig(
        r=spec.get("lora_rank", 32),
        lora_alpha=spec.get("lora_alpha", 64),
        lora_dropout=spec.get("lora_dropout", 0.05),
        bias="none",
        task_type="CAUSAL_LM",
        target_modules="all-linear",
    )

    stage(f"tokenizing {train_file.name}")
    train_dataset = build_dataset(load_jsonl(train_file), tokenizer)
    eval_dataset = (
        build_dataset(load_jsonl(eval_file), tokenizer)
        if eval_file is not None and eval_file.is_file()
        else None
    )
    stage(
        f"{len(train_dataset)} training examples, "
        f"{len(eval_dataset) if eval_dataset is not None else 0} for eval"
    )

    has_eval = eval_dataset is not None and len(eval_dataset) > 0
    if eval_dataset is not None and not has_eval:
        # A zero-row eval set is not None, so it would turn evaluation on
        # and then evaluate nothing: no eval_loss, no exception, and a run
        # that reports success having measured itself against an empty
        # file.
        stage("the eval file has no usable examples — skipping evaluation")
        eval_dataset = None

    config = SFTConfig(**build_sft_kwargs(spec, out_dir, has_eval))

    trainer = SFTTrainer(
        model=model,
        args=config,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        peft_config=peft_config,
    )
    trainer.add_callback(_progress_reporter_class()())
    # Unconditionally: a run without evaluation still loses every completed
    # epoch to a stopped container, a host reboot, or an OOM on a
    # max-length training batch, and its checkpoints directory is empty for
    # the same reason last night's was.
    trainer.add_callback(_epoch_saver_class(out_dir, announce_eval=has_eval)())
    started = time.time()
    stage("training starts now — a line lands every step")
    train_output = trainer.train()
    stage("training finished; writing the adapter")
    trainer.model.save_pretrained(str(out_dir))
    tokenizer.save_pretrained(str(out_dir))
    stage(f"adapter written to {out_dir}")

    metrics = dict(getattr(train_output, "metrics", {}) or {})
    return {
        "status": "succeeded",
        "mode": "train",
        "adapter_dir": str(out_dir),
        "base_model": spec["base_model"],
        "train_examples": len(train_dataset),
        "eval_examples": len(eval_dataset) if eval_dataset is not None else 0,
        "duration_seconds": int(time.time() - started),
        "metrics": metrics,
    }


# ----------------------------------------------------------------- merge


def convert_to_gguf(merged_dir: Path, out_file: Path) -> None:
    """Convert merged HF weights to GGUF at f16, and stop there.

    Quantizing is Ollama's job. ``/api/create`` takes a ``quantize`` field
    and applies it to exactly this file, so building llama.cpp's quantizer
    into this image bought a second implementation of a step that already
    worked — and shipped it broken, linked against a shared library the
    image does not carry.
    """
    converter = Path(
        os.environ.get("LLAMA_CPP_CONVERT", "/opt/llama.cpp/convert_hf_to_gguf.py")
    )
    subprocess.run(
        [
            sys.executable,
            str(converter),
            str(merged_dir),
            "--outfile",
            str(out_file),
            "--outtype",
            "f16",
        ],
        check=True,
    )


def run_merge(base_model: str, adapter: Path, out_file: Path) -> dict:
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    started = time.time()
    tokenizer = AutoTokenizer.from_pretrained(base_model, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        base_model, dtype=torch.bfloat16, device_map="cpu", trust_remote_code=True
    )
    merged = PeftModel.from_pretrained(model, str(adapter)).merge_and_unload()

    merged_dir = out_file.parent / "merged-hf"
    merged.save_pretrained(str(merged_dir), safe_serialization=True)
    tokenizer.save_pretrained(str(merged_dir))
    convert_to_gguf(merged_dir, out_file)
    return {
        "status": "succeeded",
        "mode": "merge",
        "gguf": str(out_file),
        "base_model": base_model,
        "quantization": "f16",
        "duration_seconds": int(time.time() - started),
    }


# ------------------------------------------------------------------ main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["train", "merge"], default="train")
    parser.add_argument("--spec", help="path to finetune_spec.json (train mode)")
    parser.add_argument("--base-model", default="")
    parser.add_argument("--adapter", default="")
    parser.add_argument("--out", default="")
    return parser


def _raise_on_termination() -> None:
    """Turn ``docker stop`` into an exception the run can record.

    Python's default SIGTERM handling exits the process without unwinding,
    so a cancelled run wrote no ``result.json`` at all — and cancelling is
    how a run being watched go wrong actually ends. Two hours of training
    would sit in an epoch directory with nothing anywhere saying so.
    """
    import signal

    def _handler(signum, _frame):
        # An Exception, not KeyboardInterrupt: the run's outcome handler
        # catches Exception, and BaseException would sail straight past it
        # into the exact silence this exists to close.
        raise Terminated(f"cancelled by signal {signum}")

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):  # not the main thread, or unsupported
            pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "train":
        _raise_on_termination()
        # First line of the container's life: everything before this is
        # Python starting up, and everything after is minutes long.
        stage("trainer container is up, reading the job spec")
        spec_path = resolve_in_workspace(args.spec or str(WORKSPACE / "finetune_spec.json"))
        spec = json.loads(spec_path.read_text())
        out_dir = output_dir_in_workspace(spec["output_dir"])
        try:
            result = run_training(spec)
        except Exception as exc:  # noqa: BLE001 — the run's outcome is a file
            # Nothing downstream reads this file; the console finds
            # adapters by scanning for weights, and an epoch directory is
            # deliberately somewhere it does not scan. So the record has
            # to name what the run left behind, or a recovered epoch is
            # on disk and invisible.
            recovered = newest_epoch_adapter(out_dir)
            if recovered is not None:
                stage(f"training died, but epoch adapter {recovered} survived")
            write_result(
                out_dir,
                {
                    "status": "failed",
                    "mode": "train",
                    "error": str(exc),
                    "recovered_adapter_dir": str(recovered) if recovered else "",
                },
            )
            raise
        write_result(out_dir, result)
    else:
        out_file = Path(args.out or (WORKSPACE / "merged-f16.gguf"))
        adapter = resolve_in_workspace(args.adapter)
        try:
            result = run_merge(args.base_model, adapter, out_file)
        except Exception as exc:  # noqa: BLE001
            write_result(
                out_file.parent, {"status": "failed", "mode": "merge", "error": str(exc)}
            )
            raise
        write_result(out_file.parent, result)
    print(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
