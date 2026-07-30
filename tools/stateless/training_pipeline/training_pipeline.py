#!/usr/bin/env python3
"""Stateless CLI over the training pipeline: status, curate, export, train.

Every subcommand prints one JSON object to stdout and exits non-zero on
failure, so the console can call it across a container boundary without
sharing a Python environment. Stdlib only, by design — the control plane
must be runnable anywhere the repo is mounted, and only the trainer
container needs torch.

    training_pipeline.py status
    training_pipeline.py curate --keep-per-cluster 3
    training_pipeline.py export --mode stripped --out-dir data/training_sets/v1
    training_pipeline.py train --train-file …/train.jsonl --dry-run
    training_pipeline.py runs --kind curate --limit 10
"""

from __future__ import annotations

import argparse
import json
import os
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
from core.training_runs import (  # noqa: E402
    KIND_CURATE,
    KIND_EXPORT,
    KIND_TRAIN,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    finish_run,
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


def cmd_status(args) -> dict:
    corpus = _corpus(args)
    ledger = _ledger(args)
    reap_stale_runs(ledger)
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
        )
    }
    return stats


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
        learning_rate=args.learning_rate,
        epochs=args.epochs,
        max_sequence_length=args.max_sequence_length,
    )
    if args.dry_run:
        return plan_run(spec).as_dict()

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
    reap_stale_runs(ledger)
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
        default="keep",
        help=(
            "How credentials leave the corpus. keep (default) writes the "
            "real values, because this dataset trains a local model that "
            "needs them. randomize substitutes a same-length, same-shape "
            "surrogate, so the model still learns what a token looks like "
            "without learning a real one — the right choice when a dataset "
            "leaves this machine. redact writes placeholders, which teaches "
            "the model to emit a slug where a live token belongs."
        ),
    )

    train_p = sub.add_parser("train", help="plan or launch a fine-tune")
    train_p.add_argument("--train-file", required=True)
    train_p.add_argument("--eval-file")
    train_p.add_argument("--out-dir")
    train_p.add_argument("--name", default="hive-harness-lora")
    train_p.add_argument("--base-model", default=FineTuneSpec().base_model)
    train_p.add_argument("--image", default="hive-mind-trainer:latest")
    train_p.add_argument("--lora-rank", type=int, default=32)
    train_p.add_argument("--learning-rate", type=float, default=1e-4)
    train_p.add_argument("--epochs", type=int, default=2)
    train_p.add_argument("--max-sequence-length", type=int, default=8_192)
    train_p.add_argument(
        "--dry-run", action="store_true", help="report feasibility, launch nothing"
    )

    runs_p = sub.add_parser("runs", help="list ledger entries")
    runs_p.add_argument("--kind", choices=["curate", "export", "train"])
    runs_p.add_argument("--limit", type=int, default=20)

    return parser


COMMANDS = {
    "status": cmd_status,
    "curate": cmd_curate,
    "export": cmd_export,
    "train": cmd_train,
    "runs": cmd_runs,
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
