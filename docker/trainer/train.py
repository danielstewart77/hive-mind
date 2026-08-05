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
           result to GGUF, which is what the deploy step's merge strategy
           needs and cannot do from a service image.

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
import subprocess
import sys
import time
from pathlib import Path

WORKSPACE = Path(os.environ.get("TRAINER_WORKSPACE", "/workspace"))
RESULT_NAME = "result.json"


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
    eval_file = resolve_in_workspace(spec.get("eval_file") or "")
    out_dir = output_dir_in_workspace(spec["output_dir"])

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
        build_dataset(load_jsonl(eval_file), tokenizer) if eval_file.exists() else None
    )
    stage(
        f"{len(train_dataset)} training examples, "
        f"{len(eval_dataset) if eval_dataset is not None else 0} for eval"
    )

    config = SFTConfig(
        output_dir=str(out_dir / "checkpoints"),
        num_train_epochs=spec.get("epochs", 2),
        per_device_train_batch_size=spec.get("per_device_batch_size", 1),
        gradient_accumulation_steps=spec.get("gradient_accumulation_steps", 16),
        learning_rate=spec.get("learning_rate", 1e-4),
        warmup_ratio=spec.get("warmup_ratio", 0.03),
        max_length=spec.get("max_sequence_length", 8192),
        bf16=True,
        # Every step, not every tenth. At an effective batch of sixteen a
        # step is tens of seconds, so ten of them is a five-minute silence
        # in which a working run and a hung one look the same.
        logging_steps=1,
        save_strategy="epoch",
        eval_strategy="epoch" if eval_dataset is not None else "no",
        gradient_checkpointing=True,
        # Reentrant checkpointing silently drops gradients when nothing
        # entering a checkpointed block requires grad — precisely the
        # shape of a frozen 4-bit base with LoRA adapters on top, which
        # works today only because Transformers applies an
        # input-requires-grad workaround on our behalf. Torch 2.9 is also
        # the release that turns the unset default into a hard error.
        gradient_checkpointing_kwargs={"use_reentrant": False},
        report_to=[],
        seed=spec.get("seed", 17),
        dataset_text_field="text",
        # A progress bar redraws one line with carriage returns, which is
        # a line that never ends and therefore never appears in `docker
        # logs`. Plain per-step lines are the only form a log reader sees.
        disable_tqdm=True,
    )

    trainer = SFTTrainer(
        model=model,
        args=config,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        peft_config=peft_config,
    )
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


def convert_to_gguf(merged_dir: Path, out_file: Path, quantization: str) -> None:
    """Convert merged HF weights to GGUF, then quantize to the target type.

    llama.cpp's converter writes f16; the quantizer turns that into the type
    Ollama will serve. Two steps, because a direct convert-to-q4 loses the
    importance matrix the quantizer applies.
    """
    converter = Path(
        os.environ.get("LLAMA_CPP_CONVERT", "/opt/llama.cpp/convert_hf_to_gguf.py")
    )
    intermediate = out_file.with_name(out_file.stem + "-f16.gguf")
    subprocess.run(
        [
            sys.executable,
            str(converter),
            str(merged_dir),
            "--outfile",
            str(intermediate),
            "--outtype",
            "f16",
        ],
        check=True,
    )
    quantizer = Path(os.environ.get("LLAMA_CPP_QUANTIZE", "/opt/llama.cpp/llama-quantize"))
    subprocess.run(
        [str(quantizer), str(intermediate), str(out_file), quantization], check=True
    )
    intermediate.unlink(missing_ok=True)


def run_merge(base_model: str, adapter: Path, out_file: Path, quantization: str) -> dict:
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
    convert_to_gguf(merged_dir, out_file, quantization)
    return {
        "status": "succeeded",
        "mode": "merge",
        "gguf": str(out_file),
        "base_model": base_model,
        "quantization": quantization,
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
    parser.add_argument("--quantization", default="q4_K_M")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "train":
        # First line of the container's life: everything before this is
        # Python starting up, and everything after is minutes long.
        stage("trainer container is up, reading the job spec")
        spec_path = resolve_in_workspace(args.spec or str(WORKSPACE / "finetune_spec.json"))
        spec = json.loads(spec_path.read_text())
        out_dir = output_dir_in_workspace(spec["output_dir"])
        try:
            result = run_training(spec)
        except Exception as exc:  # noqa: BLE001 — the run's outcome is a file
            write_result(
                out_dir, {"status": "failed", "mode": "train", "error": str(exc)}
            )
            raise
        write_result(out_dir, result)
    else:
        out_file = Path(args.out or (WORKSPACE / "merged.gguf"))
        adapter = resolve_in_workspace(args.adapter)
        try:
            result = run_merge(args.base_model, adapter, out_file, args.quantization)
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
