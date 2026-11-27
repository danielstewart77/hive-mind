"""Training pipeline router for hive-tools.

The training corpus, the curation engine and the fine-tune launcher all live
in the hive_mind repo, because that is where the capture hook writes and
where the data contract is kept. The console lives in its own container and
cannot import any of it. This router is the seam between them.

It does not reimplement the pipeline. It shells out to hive_mind's stateless
CLI — ``tools/stateless/training_pipeline/training_pipeline.py``, stdlib
only by design — and passes the JSON straight back. Every behavioural
decision therefore stays in one place, next to the data and its tests, and
this file stays a transport.

Endpoints mirror the CLI subcommands:

- ``GET  /training/status``  — corpus counts, GPU state, latest run per kind
- ``GET  /training/runs``    — ledger history
- ``POST /training/curate``  — run a curation pass (or reset verdicts)
- ``POST /training/export``  — write train/eval JSONL
- ``POST /training/train``   — plan or launch a LoRA fine-tune

Curation and export are minutes-long on the real corpus, so both run in a
worker thread with a generous timeout rather than blocking the event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth_api import require_api_token_or_administrator as require_api_token
from hitl import hitl_gate
from tools import training_gpu

log = logging.getLogger(__name__)

router = APIRouter(prefix="/training", tags=["training"])

# The repo is bind-mounted at its host path so that paths handed to the
# Docker daemon resolve identically inside and out. Defaulting to this
# process's home directory instead — as this once did — means a rebuild
# from source produces a container where every training route answers 503,
# while the running one works.
HIVE_MIND_DIR = Path(
    os.environ.get("HIVE_MIND_DIR", "/home/daniel/Storage/Dev/hive_mind")
)
CLI_RELATIVE = Path("tools/stateless/training_pipeline/training_pipeline.py")

# Deploy targets the Ollama on the machine that did the training, because
# that is where the adapter files are and where the GPU that produced them
# lives. That is this host, not the shared inference box other tools point
# at, so it is its own setting; model calls go through the inference proxy.
OLLAMA_URL = os.environ.get("TRAINING_OLLAMA_URL", "http://host.docker.internal:11434")

# Curation walks every row and hashes it; export renders several thousand
# turns to JSONL. Both are minutes, not seconds, on the live corpus.
LONG_TIMEOUT_SECONDS = 1_800
SHORT_TIMEOUT_SECONDS = 120


def _python_binary() -> str:
    """Prefer hive_mind's venv, fall back to whatever interpreter exists.

    The CLI is stdlib-only precisely so this fallback is safe: if the venv
    has not been created on this host, a bare python3 still runs it.
    """
    venv_python = HIVE_MIND_DIR / "venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return "python3"


def _cli_path() -> Path:
    return HIVE_MIND_DIR / CLI_RELATIVE


def _run_cli(args: list[str], timeout: int) -> dict[str, Any]:
    """Invoke the pipeline CLI and return its JSON object.

    The CLI's contract is one JSON object on stdout and a non-zero exit on
    failure. A non-JSON body means the interpreter itself failed (missing
    mount, syntax error), which is worth surfacing verbatim rather than
    swallowing into a generic 500.
    """
    cli = _cli_path()
    if not cli.exists():
        raise HTTPException(
            status_code=503,
            detail=(
                f"training pipeline CLI not found at {cli}. "
                "Is the hive_mind repo mounted into hive-tools?"
            ),
        )
    command = [_python_binary(), str(cli), *args]
    log.info("training.cli.invoke cmd=%s", shlex.join(command))
    try:
        completed = subprocess.run(
            command,
            cwd=str(HIVE_MIND_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504, detail=f"training command timed out after {timeout}s"
        ) from None
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    stdout = (completed.stdout or "").strip()
    if not stdout:
        raise HTTPException(
            status_code=500,
            detail=(completed.stderr or "no output from training CLI").strip()[:2000],
        )
    try:
        payload = json.loads(stdout.splitlines()[-1])
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=500, detail=f"non-JSON output from training CLI: {stdout[:1000]}"
        ) from None
    if completed.returncode != 0:
        raise HTTPException(status_code=500, detail=payload)
    return payload


async def _run_cli_async(args: list[str], timeout: int) -> dict[str, Any]:
    return await asyncio.to_thread(_run_cli, args, timeout)


class CurateRequest(BaseModel):
    keep_per_cluster: int = Field(
        3,
        ge=1,
        le=1000,
        description=(
            "How many turns survive per near-duplicate cluster. The single "
            "most consequential knob: 1 discards genuine variety in a "
            "recurring task, unbounded lets scheduled skills dominate."
        ),
    )
    min_length_tokens: int = Field(60, ge=0)
    max_length_tokens: int = Field(32_000, ge=512)
    max_tool_error_ratio: float = Field(0.6, ge=0.0, le=1.0)
    require_tool_call: bool = False
    exclude_secret_rows: bool = False
    harness: list[str] | None = None
    reset: bool = Field(
        False, description="Return every row to pending instead of curating."
    )


class ExportRequest(BaseModel):
    mode: str = Field("stripped", pattern="^(stripped|reasoning)$")
    name: str = Field("latest", pattern=r"^[A-Za-z0-9._-]{1,64}$")
    out_dir: str | None = None
    harness: list[str] | None = None
    require_reasoning: bool = False
    eval_fraction: float = Field(0.05, ge=0.0, lt=1.0)
    include_system_prompt: bool = True
    max_tool_result_chars: int = Field(8_000, ge=0)
    secrets: str = Field(
        "randomize",
        pattern="^(keep|randomize|redact)$",
        description=(
            "How credentials leave the corpus. randomize (default, and the "
            "CLI's default) substitutes a same-length, same-shape surrogate "
            "so the model learns what a token looks like without learning a "
            "real one. keep writes the real values — deliberate only, since "
            "a LoRA over a duplicated corpus memorizes them verbatim. redact "
            "writes placeholders, which teaches the model to emit a slug "
            "where a live token belongs."
        ),
    )


class KillRequest(BaseModel):
    name: str = Field(..., pattern=r"^[A-Za-z0-9._-]{1,64}$")


class TrainRequest(BaseModel):
    train_file: str
    eval_file: str | None = None
    out_dir: str | None = None
    name: str = Field("hive-harness-lora", pattern=r"^[A-Za-z0-9._-]{1,64}$")
    base_model: str | None = Field(
        None,
        description=(
            "Catalog id from GET /training/base-models. Dense models train "
            "predictably under LoRA; mixture-of-experts models train only "
            "the experts the corpus happens to activate."
        ),
    )
    image: str | None = None
    lora_rank: int = Field(
        32,
        ge=1,
        le=512,
        description=(
            "How much capacity the adapter has. Low values learn format and "
            "tone; high values can learn content, and can also overfit a "
            "few thousand turns into memorization."
        ),
    )
    lora_alpha: int = Field(
        0, ge=0, le=1024, description="Zero means twice the rank, the usual pairing."
    )
    lora_dropout: float = Field(0.05, ge=0.0, lt=1.0)
    learning_rate: float = Field(1e-4, gt=0, lt=1)
    epochs: int = Field(
        2,
        ge=1,
        le=20,
        description=(
            "Passes over the dataset. More epochs learn the style harder and "
            "memorize the examples harder; past three, a small corpus starts "
            "reciting rather than generalizing."
        ),
    )
    batch_size: int = Field(1, ge=1, le=64)
    gradient_accumulation_steps: int = Field(16, ge=1, le=256)
    max_sequence_length: int = Field(
        8_192,
        ge=512,
        description=(
            "The longest turn the run can see. Costs VRAM roughly linearly, "
            "and anything longer than this was already dropped at curation."
        ),
    )
    warmup_ratio: float = Field(0.03, ge=0.0, lt=0.5)
    load_in_4bit: bool = Field(
        True,
        description=(
            "Compress the base to four bits while training so a larger model "
            "fits. Turn off only for a model small enough to hold in bf16 — "
            "faster and slightly more faithful when it fits."
        ),
    )
    seed: int = Field(17, ge=0)
    dry_run: bool = Field(
        True,
        description=(
            "Default true. A launch consumes the whole GPU, so starting one "
            "is an explicit act rather than the fallthrough."
        ),
    )


class DeployRequest(BaseModel):
    name: str = Field(..., pattern=r"^[A-Za-z0-9._:-]{1,64}$")
    adapter_dir: str
    base_model: str | None = None
    serve_tag: str | None = None
    quantization: str = Field(
        "q4_K_M",
        pattern=r"^[A-Za-z0-9_]{2,12}$",
        description="what Ollama quantizes the merged f16 GGUF to on import",
    )
    temperature: float = Field(0.2, ge=0.0, le=2.0)
    num_ctx: int = Field(32_768, ge=512)
    system_prompt: str = ""


@router.get("/status", dependencies=[Depends(require_api_token)])
async def training_status() -> dict[str, Any]:
    """Corpus counts, curation breakdown, GPU state, latest run per kind."""
    return await _run_cli_async(["status"], SHORT_TIMEOUT_SECONDS)


@router.get("/in-flight", dependencies=[Depends(require_api_token)])
async def training_in_flight() -> dict[str, Any]:
    """The run currently holding the card, if there is one.

    Answered from the journal rather than the ledger: the journal exists
    only between a preflight and its restore and is checked against the
    live container, so it cannot report a run that finished hours ago.
    A console loading this can attach to a run it did not start, which
    is the ordinary case after a refresh.
    """
    entry = training_gpu.run_in_flight()
    if not entry:
        return {"in_flight": False}
    return {
        "in_flight": True,
        "name": entry.get("output_name", ""),
        "run_id": entry.get("run_id", ""),
        "started_at": entry.get("started_at", 0),
    }


@router.get("/logs", dependencies=[Depends(require_api_token)])
async def training_logs(name: str, tail: int = training_gpu.LOG_TAIL_LINES) -> dict[str, Any]:
    """Recent trainer output, for watching a run without holding it.

    Polled by the console. Deliberately not a stream: a run outlives every
    tab watching it, and a socket the trainer's survival appeared to
    depend on is a socket someone will eventually be afraid to close.

    Falls back to the archive when the container is gone, so the output of
    a run that has already ended is still readable — which is when most
    people go looking for it.
    """
    if not name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    limit = max(1, min(tail, 2_000))
    result = training_gpu.trainer_logs(name.strip(), tail=limit)
    if not result.get("available"):
        archived = training_gpu.archived_logs(name.strip(), tail=limit)
        if archived.get("available"):
            result = archived
    # A trainer can go a minute between lines, and identical text for a
    # minute looks exactly like a poller that has died.
    return {
        "name": name.strip(),
        **result,
        "heartbeat": training_gpu.heartbeat(name.strip()),
        "progress": training_gpu.progress_from(result.get("lines") or []),
    }


@router.post("/kill", dependencies=[Depends(require_api_token)])
async def training_kill(body: KillRequest) -> dict[str, Any]:
    """Stop a run in flight.

    The intent is recorded before the signal is sent: exit 137 looks the
    same whether the operator stopped the run or it was killed by the
    kernel, so a stop that lands before the flag is written is a stop
    that gets filed as a failure.

    Everything after this is the watcher's ordinary path — it sees the
    container end, archives the logs, restores the voice stack and closes
    the ledger row. Nothing here restores anything itself, because two
    restore paths racing is how the voice stack comes back twice.
    """
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    entry = training_gpu.read_journal()
    if entry and entry.get("output_name") == name:
        entry["cancelled"] = True
        training_gpu.write_journal(entry)
    result = training_gpu.stop_trainer(name)
    log.info("training.kill name=%s stopped=%s", name, result["stopped"])
    return {"name": name, **result}


@router.get("/runs", dependencies=[Depends(require_api_token)])
async def training_runs(kind: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Ledger history, newest first."""
    if kind and kind not in {"curate", "export", "train"}:
        raise HTTPException(status_code=400, detail=f"unknown run kind {kind!r}")
    args = ["runs", "--limit", str(max(1, min(limit, 200)))]
    if kind:
        args += ["--kind", kind]
    return await _run_cli_async(args, SHORT_TIMEOUT_SECONDS)


@router.post("/curate")
async def training_curate(
    body: CurateRequest,
    caller: str = Depends(hitl_gate("training_curate", who=require_api_token)),
) -> dict[str, Any]:
    """Run a curation pass, writing verdicts in place. Never deletes rows."""
    del caller
    if body.reset:
        return await _run_cli_async(["curate", "--reset"], LONG_TIMEOUT_SECONDS)
    args = [
        "curate",
        "--keep-per-cluster",
        str(body.keep_per_cluster),
        "--min-length-tokens",
        str(body.min_length_tokens),
        "--max-length-tokens",
        str(body.max_length_tokens),
        "--max-tool-error-ratio",
        str(body.max_tool_error_ratio),
    ]
    if body.require_tool_call:
        args.append("--require-tool-call")
    if body.exclude_secret_rows:
        args.append("--exclude-secret-rows")
    for harness in body.harness or []:
        args += ["--harness", harness]
    return await _run_cli_async(args, LONG_TIMEOUT_SECONDS)


@router.post("/export")
async def training_export(
    body: ExportRequest,
    caller: str = Depends(hitl_gate("training_export", who=require_api_token)),
) -> dict[str, Any]:
    """Render kept rows to train/eval JSONL with credentials redacted."""
    del caller
    args = [
        "export",
        "--mode",
        body.mode,
        "--name",
        body.name,
        "--eval-fraction",
        str(body.eval_fraction),
        "--max-tool-result-chars",
        str(body.max_tool_result_chars),
    ]
    if body.out_dir:
        args += ["--out-dir", body.out_dir]
    if body.require_reasoning:
        args.append("--require-reasoning")
    if not body.include_system_prompt:
        args.append("--no-system-prompt")
    args += ["--secrets", body.secrets]
    for harness in body.harness or []:
        args += ["--harness", harness]
    return await _run_cli_async(args, LONG_TIMEOUT_SECONDS)


@router.post("/train")
async def training_train(
    body: TrainRequest,
    caller: str = Depends(hitl_gate("training_train", who=require_api_token)),
) -> dict[str, Any]:
    """Plan a fine-tune, or launch one when ``dry_run`` is false."""
    del caller
    args = [
        "train",
        "--train-file",
        body.train_file,
        "--name",
        body.name,
        "--lora-rank",
        str(body.lora_rank),
        "--lora-dropout",
        str(body.lora_dropout),
        "--learning-rate",
        str(body.learning_rate),
        "--epochs",
        str(body.epochs),
        "--batch-size",
        str(body.batch_size),
        "--gradient-accumulation-steps",
        str(body.gradient_accumulation_steps),
        "--max-sequence-length",
        str(body.max_sequence_length),
        "--warmup-ratio",
        str(body.warmup_ratio),
        "--seed",
        str(body.seed),
    ]
    if body.lora_alpha:
        args += ["--lora-alpha", str(body.lora_alpha)]
    if not body.load_in_4bit:
        args.append("--no-4bit")
    if body.eval_file:
        args += ["--eval-file", body.eval_file]
    if body.out_dir:
        args += ["--out-dir", body.out_dir]
    if body.base_model:
        args += ["--base-model", body.base_model]
    if body.image:
        args += ["--image", body.image]
    if body.dry_run:
        args.append("--dry-run")
        # The planner sees a card still holding the voice stack and
        # Ollama's models. A launch takes both back before the trainer
        # starts, so the honest number to judge against includes them.
        args += ["--reclaimable-mib", str(await training_gpu.reclaimable_mib())]
        return await _run_cli_async(args, SHORT_TIMEOUT_SECONDS)

    return await launch_with_gpu(body.name, args)


#: How a watcher's outcome word maps onto a ledger status. "ended, outcome
#: unknown" is deliberately absent — a run nobody can describe is left open
#: rather than guessed at, and the reaper decides its fate on age.
_OUTCOME_STATUS = {
    "succeeded": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
}


async def close_ledger_run(run_id: str, outcome: str, exit_code: int | None) -> None:
    """Write a finished run's outcome into the ledger.

    The launching request returned minutes or hours ago, so nothing else
    is left holding this run — without this the row stays ``running``
    forever and the reaper eventually files a completed run as a failure.
    """
    status = _OUTCOME_STATUS.get(outcome)
    if not status:
        return
    args = ["close-run", "--run-id", run_id, "--status", status]
    if status != "succeeded":
        args += ["--error", f"{outcome}" + (f" (exit {exit_code})" if exit_code is not None else "")]
    await _run_cli_async(args, SHORT_TIMEOUT_SECONDS)


async def launch_with_gpu(output_name: str, args: list[str]) -> dict[str, Any]:
    """Take the card, launch, and arm the watcher that gives it back.

    Every real launch goes through here — the console button and an agent
    calling the same route are the same code path, so neither can acquire
    the GPU by a route that forgets to release it.
    """
    in_flight = training_gpu.run_in_flight()
    if in_flight:
        # One card, one trainer. Two runs would each preflight over the
        # other's record and the first to finish would restore the voice
        # stack on top of the second.
        raise HTTPException(
            status_code=409,
            detail=(
                f"a training run is already in flight: {in_flight.get('output_name')} "
                f"(started {in_flight.get('started_at')})"
            ),
        )

    entry = await training_gpu.preflight(output_name)
    try:
        result = await _run_cli_async(args, SHORT_TIMEOUT_SECONDS)
    except Exception:
        # Includes the CLI timing out. Nothing is training, so the card
        # goes straight back rather than staying dark until someone
        # notices the machine has stopped talking.
        await training_gpu.restore(entry)
        raise

    if not result.get("launched"):
        await training_gpu.restore(entry)
        return result

    entry["run_id"] = result.get("run_id", "")
    training_gpu.write_journal(entry)
    asyncio.ensure_future(
        training_gpu.watch(
            entry, notify=training_gpu.notify_owner, close_run=close_ledger_run
        )
    )
    # The watcher announces the end. Nothing announced the start, so the
    # first anyone knew of the card being taken was the voice going silent.
    await training_gpu.safe_notify(
        training_gpu.notify_owner,
        f"training run {output_name} started; voice is down until it ends",
    )
    return result


@router.get("/base-models", dependencies=[Depends(require_api_token)])
async def training_base_models() -> dict[str, Any]:
    """Candidate bases, annotated with what this host can train and serve."""
    args = ["base-models"]
    if OLLAMA_URL:
        args += ["--ollama-url", OLLAMA_URL]
    return await _run_cli_async(args, SHORT_TIMEOUT_SECONDS)


@router.post("/deploy")
async def training_deploy(
    body: DeployRequest,
    caller: str = Depends(hitl_gate("training_deploy", who=require_api_token)),
) -> dict[str, Any]:
    """Serve a trained adapter, or start the merge that will let us."""
    del caller
    in_flight = training_gpu.run_in_flight()
    if in_flight:
        # A merge launches a GPU container and an import makes Ollama load
        # the result, so deploying last week's adapter mid-run collides
        # with this week's.
        raise HTTPException(
            status_code=409,
            detail=(
                "a training run is in flight "
                f"({in_flight.get('output_name')}); deploying would take VRAM "
                "from it"
            ),
        )
    args = [
        "deploy",
        "--name",
        body.name,
        "--adapter-dir",
        body.adapter_dir,
        "--quantization",
        body.quantization,
        "--temperature",
        str(body.temperature),
        "--num-ctx",
        str(body.num_ctx),
    ]
    if body.base_model:
        args += ["--base-model", body.base_model]
    if body.serve_tag:
        args += ["--serve-tag", body.serve_tag]
    if body.system_prompt:
        args += ["--system-prompt", body.system_prompt]
    if OLLAMA_URL:
        args += ["--ollama-url", OLLAMA_URL]
    # The first call launches a merge container and returns; the second
    # uploads a full model and waits while Ollama quantizes it. Neither is
    # a quick call.
    return await _run_cli_async(args, LONG_TIMEOUT_SECONDS)
