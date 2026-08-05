#!/usr/bin/env python3
"""Stateless CLI over the training pipeline: curate, export, train, deploy.

Every subcommand prints one JSON object to stdout and exits non-zero on
failure, so the console can call it across a container boundary without
sharing a Python environment. Stdlib only, by design — the control plane
must be runnable anywhere the repo is mounted, and only the trainer
container needs torch.

    training_pipeline.py status
    training_pipeline.py curate --keep-per-cluster 3
    training_pipeline.py export --mode stripped --out-dir data/training_sets/v1
    training_pipeline.py train --train-file …/train.jsonl --dry-run
    training_pipeline.py base-models
    training_pipeline.py deploy --name skippy-lora --adapter-dir …/adapter
    training_pipeline.py runs --kind curate --limit 10
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.training_curation import (  # noqa: E402
    CurationPolicy,
    corpus_stats,
    curate,
    reset_verdicts,
)
from core.training_export import ExportOptions, export_dataset  # noqa: E402
from core.training_finetune import (  # noqa: E402
    FineTuneSpec,
    launch,
    plan_run,
    read_gpu_state,
)
from core.training_deploy import (  # noqa: E402
    DEFAULT_NUM_CTX,
    DEFAULT_QUANTIZATION,
    DEFAULT_TEMPERATURE,
    STRATEGIES,
    STRATEGY_ADAPTER,
    DeployRequest,
    adapter_weight_file,
    deploy,
    merged_gguf_path,
)
from core.training_models import (  # noqa: E402
    DEFAULT_BASE_MODEL_ID,
    catalog,
)
from core.training_models import get as get_base_model  # noqa: E402
from core.training_runs import (  # noqa: E402
    KIND_CURATE,
    KIND_DEPLOY,
    KIND_EXPORT,
    KIND_TRAIN,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    TERMINAL_STATUSES,
    finish_run,
    get_run,
    latest_run,
    list_runs,
    reap_stale_runs,
    start_run,
)

DEFAULT_CORPUS = REPO_ROOT / "data" / "training_turns.db"
DEFAULT_LEDGER = REPO_ROOT / "data" / "training_runs.db"
DEFAULT_SETS_DIR = REPO_ROOT / "data" / "training_sets"


def _corpus(args) -> Path:
    return Path(args.corpus or os.getenv("TRAINING_CORPUS_DB") or DEFAULT_CORPUS)


def _ledger(args) -> Path:
    return Path(args.ledger or os.getenv("TRAINING_RUNS_DB") or DEFAULT_LEDGER)


def _trainer_is_alive(run) -> bool:
    """True when this run's trainer container still exists and is running.

    Consulted before the reaper fails an over-age run. Docker being
    unreachable answers *alive*: mistaking a live trainer for a dead one
    closes its ledger row, and everything keyed to that row then acts on a
    GPU the trainer is still holding.
    """
    name = (run.options or {}).get("output_name")
    if run.kind != KIND_TRAIN or not name:
        return False
    try:
        completed = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", f"hive-trainer-{name}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return True
    if completed.returncode != 0:
        # docker answered, and it has no such container: the trainer is gone.
        return False
    return completed.stdout.strip() == "true"


def cmd_status(args) -> dict:
    corpus = _corpus(args)
    ledger = _ledger(args)
    reap_stale_runs(ledger, is_alive=_trainer_is_alive)
    if not corpus.exists():
        return {"corpus_path": str(corpus), "exists": False}
    stats = corpus_stats(corpus)
    stats["corpus_path"] = str(corpus)
    stats["exists"] = True
    stats["gpu"] = read_gpu_state().__dict__
    stats["latest"] = {
        kind: (run.as_dict() if run else None)
        for kind, run in (
            (KIND_CURATE, latest_run(ledger, KIND_CURATE)),
            (KIND_EXPORT, latest_run(ledger, KIND_EXPORT)),
            (KIND_TRAIN, latest_run(ledger, KIND_TRAIN)),
            (KIND_DEPLOY, latest_run(ledger, KIND_DEPLOY)),
        )
    }
    stats["datasets"] = _datasets()
    stats["adapters"] = _adapters()
    return stats


def _datasets() -> list[dict]:
    """Exported datasets on disk, newest first.

    The train form asks for a path to a JSONL file, which is an unfair
    question to put in front of anyone who has only ever pressed Export.
    Listing what exists turns it into a choice.
    """
    rows = []
    if not DEFAULT_SETS_DIR.is_dir():
        return rows
    for directory in sorted(DEFAULT_SETS_DIR.iterdir()):
        train_file = directory / "train.jsonl"
        if not train_file.is_file():
            continue
        eval_file = directory / "eval.jsonl"
        rows.append(
            {
                "name": directory.name,
                "train_file": str(train_file),
                "eval_file": str(eval_file) if eval_file.is_file() else "",
                "train_examples": _count_lines(train_file),
                "eval_examples": _count_lines(eval_file) if eval_file.is_file() else 0,
                "modified_at": int(train_file.stat().st_mtime),
            }
        )
    rows.sort(key=lambda row: row["modified_at"], reverse=True)
    return rows


def _count_lines(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def _adapters() -> list[dict]:
    """Trained adapters on disk, so deploy can offer a list rather than a path."""
    rows = []
    if not DEFAULT_SETS_DIR.is_dir():
        return rows
    # Spec files are named per job (finetune_spec.<name>.json) so two runs
    # sharing a dataset directory cannot overwrite each other's; the older
    # single-name form is still matched for datasets trained before that.
    for spec_path in sorted(DEFAULT_SETS_DIR.glob("*/finetune_spec*.json")):
        adapter_dir = spec_path.parent / "adapter"
        try:
            spec = json.loads(spec_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        weights = adapter_weight_file(adapter_dir)
        rows.append(
            {
                "name": spec.get("output_name") or spec_path.parent.name,
                "dataset": spec_path.parent.name,
                "adapter_dir": str(adapter_dir),
                "base_model": spec.get("base_model", ""),
                "trained": weights is not None,
                "merged_gguf": str(merged_gguf_path(adapter_dir))
                if merged_gguf_path(adapter_dir).is_file()
                else "",
                "modified_at": int(spec_path.stat().st_mtime),
            }
        )
    rows.sort(key=lambda row: row["modified_at"], reverse=True)
    return rows


def cmd_base_models(args) -> dict:
    """The catalog, annotated with what this host can train and serve today."""
    gpu = read_gpu_state()
    free = gpu.free_mib if gpu.available else None
    return {
        "default": DEFAULT_BASE_MODEL_ID,
        "gpu": gpu.__dict__,
        "models": catalog(free_vram_mib=free, ollama_url=args.ollama_url),
    }


def cmd_deploy(args) -> dict:
    ledger = _ledger(args)
    base = get_base_model(args.base_model) if args.base_model else None
    serve_tag = args.serve_tag or (base.serve_tag if base else "")
    request = DeployRequest(
        model_name=args.name,
        adapter_dir=args.adapter_dir,
        serve_tag=serve_tag,
        strategy=args.strategy,
        base_model=args.base_model or (base.id if base else ""),
        quantization=args.quantization,
        temperature=args.temperature,
        num_ctx=args.num_ctx,
        system_prompt=args.system_prompt or "",
    )
    run_id = start_run(ledger, KIND_DEPLOY, options=request.as_dict())
    result = deploy(request, ollama_url=args.ollama_url, trainer_image=args.image)
    if result.deployed:
        finish_run(
            ledger, run_id, status=STATUS_SUCCEEDED, report=result.as_dict()
        )
    elif result.stage == "converting":
        # Left open on purpose: the merge container is still working, and
        # the reaper or the next deploy call closes the row.
        pass
    else:
        finish_run(
            ledger,
            run_id,
            status=STATUS_FAILED,
            report=result.as_dict(),
            error="; ".join(result.blockers) or "deploy refused",
        )
    return {"run_id": run_id, **result.as_dict()}


def cmd_curate(args) -> dict:
    corpus = _corpus(args)
    ledger = _ledger(args)
    if args.reset:
        return {"reset_rows": reset_verdicts(corpus)}
    policy = CurationPolicy(
        min_length_tokens=args.min_length_tokens,
        max_length_tokens=args.max_length_tokens,
        keep_per_cluster=args.keep_per_cluster,
        max_tool_error_ratio=args.max_tool_error_ratio,
        require_tool_call=args.require_tool_call,
        exclude_secret_rows=args.exclude_secret_rows,
        harnesses=tuple(args.harness or ()),
    )
    run_id = start_run(ledger, KIND_CURATE, options=policy.__dict__)
    try:
        report = curate(corpus, policy)
    except Exception as exc:
        finish_run(ledger, run_id, status=STATUS_FAILED, error=str(exc))
        raise
    finish_run(ledger, run_id, status=STATUS_SUCCEEDED, report=report.as_dict())
    return {"run_id": run_id, **report.as_dict()}


def cmd_export(args) -> dict:
    corpus = _corpus(args)
    ledger = _ledger(args)
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_SETS_DIR / args.name
    options = ExportOptions(
        mode=args.mode,
        harnesses=tuple(args.harness or ()),
        require_reasoning=args.require_reasoning,
        eval_fraction=args.eval_fraction,
        include_system_prompt=not args.no_system_prompt,
        max_tool_result_chars=args.max_tool_result_chars,
        secrets=args.secrets,
    )
    run_id = start_run(ledger, KIND_EXPORT, options=options.__dict__)
    try:
        report = export_dataset(corpus, out_dir, options)
    except Exception as exc:
        finish_run(ledger, run_id, status=STATUS_FAILED, error=str(exc))
        raise
    finish_run(
        ledger,
        run_id,
        status=STATUS_SUCCEEDED,
        report=report.as_dict(),
        artifact_path=str(out_dir),
    )
    return {"run_id": run_id, **report.as_dict()}


def cmd_train(args) -> dict:
    ledger = _ledger(args)
    train_file = Path(args.train_file)
    out_dir = Path(args.out_dir) if args.out_dir else train_file.parent
    spec = FineTuneSpec(
        base_model=args.base_model,
        output_name=args.name,
        train_file=str(train_file),
        eval_file=str(args.eval_file or (train_file.parent / "eval.jsonl")),
        output_dir=str(out_dir / "adapter"),
        lora_rank=args.lora_rank,
        lora_alpha=args.lora_alpha if args.lora_alpha else args.lora_rank * 2,
        lora_dropout=args.lora_dropout,
        learning_rate=args.learning_rate,
        epochs=args.epochs,
        per_device_batch_size=args.batch_size,
        per_device_eval_batch_size=args.eval_batch_size,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        max_sequence_length=args.max_sequence_length,
        warmup_ratio=args.warmup_ratio,
        load_in_4bit=not args.no_4bit,
        seed=args.seed,
    )
    if args.dry_run:
        return plan_run(spec, reclaimable_mib=args.reclaimable_mib).as_dict()

    run_id = start_run(ledger, KIND_TRAIN, options=spec.as_dict())
    result = launch(spec, out_dir, image=args.image)
    if not result.get("launched"):
        finish_run(
            ledger,
            run_id,
            status=STATUS_FAILED,
            report=result,
            error="; ".join(result.get("plan", {}).get("blockers", []))
            or result.get("error", "launch refused"),
        )
    else:
        # The container runs detached; the run stays open until a later
        # status poll or the reaper closes it.
        pass
    return {"run_id": run_id, **result}


def cmd_runs(args) -> dict:
    ledger = _ledger(args)
    reap_stale_runs(ledger, is_alive=_trainer_is_alive)
    return {
        "runs": [run.as_dict() for run in list_runs(ledger, args.kind, args.limit)]
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", help="path to training_turns.db")
    parser.add_argument("--ledger", help="path to training_runs.db")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="corpus counts, GPU state, latest runs")

    curate_p = sub.add_parser("curate", help="run a curation pass")
    curate_p.add_argument("--min-length-tokens", type=int, default=60)
    curate_p.add_argument("--max-length-tokens", type=int, default=32_000)
    curate_p.add_argument("--keep-per-cluster", type=int, default=3)
    curate_p.add_argument("--max-tool-error-ratio", type=float, default=0.6)
    curate_p.add_argument("--require-tool-call", action="store_true")
    curate_p.add_argument("--exclude-secret-rows", action="store_true")
    curate_p.add_argument("--harness", action="append")
    curate_p.add_argument(
        "--reset", action="store_true", help="return every row to pending"
    )

    export_p = sub.add_parser("export", help="write train/eval JSONL")
    export_p.add_argument("--mode", choices=["stripped", "reasoning"], default="stripped")
    export_p.add_argument("--name", default="latest")
    export_p.add_argument("--out-dir")
    export_p.add_argument("--harness", action="append")
    export_p.add_argument("--require-reasoning", action="store_true")
    export_p.add_argument("--eval-fraction", type=float, default=0.05)
    export_p.add_argument("--no-system-prompt", action="store_true")
    export_p.add_argument("--max-tool-result-chars", type=int, default=8_000)
    export_p.add_argument(
        "--secrets",
        choices=["keep", "randomize", "redact"],
        default="randomize",
        help=(
            "How credentials leave the corpus. randomize (default) "
            "substitutes a same-length, same-shape surrogate, so the model "
            "learns what a token looks like without learning a real one. "
            "keep writes the real values — deliberate only, since a small "
            "LoRA over a duplicated corpus memorizes them verbatim. redact "
            "writes placeholders, which teaches the model to emit a slug "
            "where a live token belongs."
        ),
    )

    train_p = sub.add_parser("train", help="plan or launch a fine-tune")
    train_p.add_argument("--train-file", required=True)
    train_p.add_argument("--eval-file")
    train_p.add_argument("--out-dir")
    train_p.add_argument("--name", default="hive-harness-lora")
    train_p.add_argument("--base-model", default=DEFAULT_BASE_MODEL_ID)
    train_p.add_argument("--image", default="hive-mind-trainer:latest")
    train_p.add_argument("--lora-rank", type=int, default=32)
    train_p.add_argument(
        "--lora-alpha",
        type=int,
        default=0,
        help="scaling for the adapter's contribution; defaults to twice the rank",
    )
    train_p.add_argument("--lora-dropout", type=float, default=0.05)
    train_p.add_argument("--learning-rate", type=float, default=1e-4)
    train_p.add_argument("--epochs", type=int, default=2)
    train_p.add_argument("--batch-size", type=int, default=1)
    # Unset means "match the training batch". Naming it larger is how
    # Transformers' own default of 8 took the card down; the feasibility
    # estimate now sizes against whichever of the two is bigger.
    train_p.add_argument("--eval-batch-size", type=int, default=None)
    train_p.add_argument("--gradient-accumulation-steps", type=int, default=16)
    train_p.add_argument("--max-sequence-length", type=int, default=8_192)
    train_p.add_argument("--warmup-ratio", type=float, default=0.03)
    train_p.add_argument(
        "--no-4bit",
        action="store_true",
        help="load the base in bf16 instead of 4-bit — faster and more "
        "faithful, and only possible on a model small enough to fit",
    )
    train_p.add_argument("--seed", type=int, default=17)
    train_p.add_argument(
        "--dry-run", action="store_true", help="report feasibility, launch nothing"
    )
    train_p.add_argument(
        "--reclaimable-mib",
        type=int,
        default=0,
        help="VRAM a real launch would free before starting — the caller "
        "owning the arbitration knows this, the planner does not",
    )

    models_p = sub.add_parser(
        "base-models", help="candidate base models, annotated for this host"
    )
    models_p.add_argument("--ollama-url", help="where to check which tags are pulled")

    deploy_p = sub.add_parser("deploy", help="serve a trained adapter through Ollama")
    deploy_p.add_argument("--name", required=True, help="the Ollama model name to create")
    deploy_p.add_argument("--adapter-dir", required=True)
    deploy_p.add_argument("--base-model", default="", help="catalog id it was trained from")
    deploy_p.add_argument(
        "--serve-tag", default="", help="override the catalog's Ollama base tag"
    )
    deploy_p.add_argument("--strategy", choices=list(STRATEGIES), default=STRATEGY_ADAPTER)
    deploy_p.add_argument("--quantization", default=DEFAULT_QUANTIZATION)
    deploy_p.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    deploy_p.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX)
    deploy_p.add_argument("--system-prompt", default="")
    deploy_p.add_argument("--image", default="hive-mind-trainer:latest")
    deploy_p.add_argument("--ollama-url", help="Ollama endpoint to create the model on")

    runs_p = sub.add_parser("runs", help="list ledger entries")
    runs_p.add_argument("--kind", choices=["curate", "export", "train", "deploy"])
    runs_p.add_argument("--limit", type=int, default=20)

    close_p = sub.add_parser("close-run", help="close an open ledger entry")
    close_p.add_argument("--run-id", required=True)
    close_p.add_argument(
        "--status", required=True, choices=sorted(TERMINAL_STATUSES)
    )
    close_p.add_argument("--error", default="")
    close_p.add_argument("--artifact-path", default="")

    return parser


def cmd_close_run(args) -> dict:
    """Close a run someone else was watching.

    The trainer runs detached and the process that launched it is long
    gone by the time it ends, so whoever *is* watching needs a way to
    write the outcome down. Closing an already-closed run is reported,
    not raised: a watcher retrying after a dropped call must not turn a
    recorded outcome into an error.
    """
    ledger = _ledger(args)
    before = get_run(ledger, args.run_id)
    if before is None:
        return {"run_id": args.run_id, "closed": False, "reason": "no such run"}
    if before.status != STATUS_RUNNING:
        return {
            "run_id": args.run_id,
            "closed": False,
            "reason": f"already {before.status}",
            "status": before.status,
        }
    finish_run(
        ledger,
        args.run_id,
        status=args.status,
        error=args.error or None,
        artifact_path=args.artifact_path or None,
    )
    return {"run_id": args.run_id, "closed": True, "status": args.status}


COMMANDS = {
    "status": cmd_status,
    "curate": cmd_curate,
    "export": cmd_export,
    "train": cmd_train,
    "base-models": cmd_base_models,
    "deploy": cmd_deploy,
    "runs": cmd_runs,
    "close-run": cmd_close_run,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = COMMANDS[args.command](args)
    except Exception as exc:  # noqa: BLE001 — the CLI's contract is JSON, always
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "type": type(exc).__name__,
                    "traceback": traceback.format_exc()[-2000:],
                }
            )
        )
        return 1
    print(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
