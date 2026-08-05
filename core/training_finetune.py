"""Fine-tune job definition and launcher.

The hive's GPU is a single RTX A6000 shared with inference. Training is
therefore never run in-process: this module *describes* a job and hands it
to a container, so a long LoRA run cannot take the console down with it and
so the trainer's heavyweight dependencies (torch, peft, transformers) stay
out of every other service's image.

The launcher is deliberately thin. It writes a job spec next to the dataset
and starts a container with the GPU attached; the container's entrypoint
reads the spec. That split means the job spec is inspectable and re-runnable
by hand, which matters a great deal the first time a run fails at 3am.

:func:`plan_run` is pure and always available. :func:`launch` needs Docker
and a free GPU, and reports a structured refusal when either is missing
rather than raising deep inside a subprocess call.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

# LoRA rather than a full fine-tune: a 30B model in bf16 does not fit in
# 48 GB alongside anything else, and harness-driving is a style-and-format
# adaptation, which is what low-rank adapters are good at. A full fine-tune
# would also be far more likely to erode the base model's general coding
# ability, which is the thing we are trying to keep.
DEFAULT_BASE_MODEL = "Qwen/Qwen3-30B-A3B-Instruct-2507"
DEFAULT_TRAINER_IMAGE = "hive-mind-trainer:latest"


@dataclass
class FineTuneSpec:
    """Everything the trainer container needs, and nothing it does not."""

    base_model: str = DEFAULT_BASE_MODEL
    output_name: str = "hive-harness-lora"
    train_file: str = ""
    eval_file: str = ""
    output_dir: str = ""

    lora_rank: int = 32
    lora_alpha: int = 64
    lora_dropout: float = 0.05
    learning_rate: float = 1e-4
    epochs: int = 2
    per_device_batch_size: int = 1
    gradient_accumulation_steps: int = 16
    max_sequence_length: int = 8_192
    warmup_ratio: float = 0.03
    load_in_4bit: bool = True
    seed: int = 17

    #: Hard ceiling on the fraction of the card this job may allocate,
    #: applied inside the trainer by ``torch.cuda.set_per_process_memory_
    #: fraction``. Planning arithmetic cannot enforce a reserve — the
    #: allocator grows past any estimate hours in, and on this host the
    #: display server is on the same card, so the desktop is what dies.
    #: Zero means no cap, which is only correct on a headless machine.
    gpu_memory_fraction: float = 0.0

    def validate(self) -> list[str]:
        """Return human-readable problems; empty means the spec is usable."""
        problems: list[str] = []
        train_path = Path(self.train_file) if self.train_file else None
        if not self.train_file or train_path is None or not train_path.exists():
            problems.append(f"train file not found: {self.train_file!r}")
        elif train_path.stat().st_size == 0:
            # A curation pass that kept nothing still writes a file. The
            # trainer would exit zero having learned nothing, and the run
            # would be reported as a success.
            problems.append(f"train file is empty: {self.train_file!r}")
        if self.eval_file and not Path(self.eval_file).exists():
            problems.append(f"eval file not found: {self.eval_file!r}")
        if not 0.0 <= self.gpu_memory_fraction <= 1.0:
            problems.append("gpu_memory_fraction must be between 0 and 1")
        if self.epochs < 1:
            problems.append("epochs must be at least 1")
        if not 0 < self.learning_rate < 1:
            problems.append("learning_rate must be between 0 and 1")
        if self.lora_rank < 1:
            problems.append("lora_rank must be at least 1")
        if self.max_sequence_length < 512:
            problems.append("max_sequence_length must be at least 512")
        return problems

    def effective_batch_size(self) -> int:
        return self.per_device_batch_size * self.gradient_accumulation_steps

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class GpuState:
    total_mib: int = 0
    used_mib: int = 0
    name: str = ""
    available: bool = False

    @property
    def free_mib(self) -> int:
        return max(0, self.total_mib - self.used_mib)


def read_gpu_state() -> GpuState:
    """Query nvidia-smi. A missing binary is a state, not an error."""
    binary = shutil.which("nvidia-smi")
    if not binary:
        return GpuState()
    try:
        output = subprocess.run(
            [
                binary,
                "--query-gpu=name,memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return GpuState()
    first = output.splitlines()[0] if output else ""
    parts = [p.strip() for p in first.split(",")]
    if len(parts) != 3:
        return GpuState()
    try:
        return GpuState(
            name=parts[0],
            total_mib=int(parts[1]),
            used_mib=int(parts[2]),
            available=True,
        )
    except ValueError:
        return GpuState()


#: Fallback weight footprints, in MiB, for a base the catalog does not know.
#: Sized for the largest model this hardware can train, because refusing a
#: run that would have fit is recoverable and an OOM at hour three is not.
UNKNOWN_MODEL_4BIT_MIB = 22_000
UNKNOWN_MODEL_BF16_MIB = 60_000

#: VRAM this host never lets a training job claim, because Xorg and
#: gnome-shell are on the same card and a failed surface allocation takes
#: the desktop — and every terminal, tile and chat window on it — with it.
#: Enforced as a cap inside the trainer process, not merely subtracted in a
#: planning function: the allocator's peak arrives hours in, at the first
#: max-length batch and again at the eval pass.
DESKTOP_RESERVE_MIB = 2_048


def memory_fraction_for(total_mib: int, reserve_mib: int = DESKTOP_RESERVE_MIB) -> float:
    """Fraction of the card the trainer may allocate, leaving the reserve.

    Returns 0.0 for an unknown card size, which the trainer reads as "no
    cap" — refusing to run because nvidia-smi was unreadable would be a
    worse failure than running uncapped on a machine that may be headless.
    """
    if total_mib <= 0:
        return 0.0
    usable = max(0, total_mib - reserve_mib)
    return round(usable / total_mib, 4)


def estimate_required_mib(spec: FineTuneSpec) -> int:
    """Rough VRAM floor for a LoRA over this base at this sequence length.

    Deliberately a crude linear model. Its job is to stop a run that will
    certainly OOM twenty minutes in, not to predict allocation precisely —
    the trainer reports the truth once it starts.

    The weight term comes from the catalog, because it is the base model
    that decides it. A single constant here — which is what this was —
    charged an 8B model the footprint of a 30B one and refused runs that
    would have fitted twice over.
    """
    from core.training_models import get as _get_base_model

    base = _get_base_model(spec.base_model)
    if base is not None:
        # The catalog quotes weights under 4-bit loading. bf16 holds four
        # times the bytes per parameter; three times is the conservative
        # rounding of that difference plus its optimizer overhead.
        weights = (
            base.weights_vram_mib if spec.load_in_4bit else base.weights_vram_mib * 3
        )
    else:
        weights = (
            UNKNOWN_MODEL_4BIT_MIB if spec.load_in_4bit else UNKNOWN_MODEL_BF16_MIB
        )
    activations = int(spec.max_sequence_length * spec.per_device_batch_size * 1.6)
    return weights + activations


@dataclass
class LaunchPlan:
    """The decision to run or not, with the reasoning attached."""

    spec: dict = field(default_factory=dict)
    gpu: dict = field(default_factory=dict)
    required_mib: int = 0
    can_run: bool = False
    blockers: list[str] = field(default_factory=list)
    reclaimable_mib: int = 0
    """VRAM a launch will take back before the trainer starts."""

    notes: list[str] = field(default_factory=list)
    """What the verdict is counting on. Populated only when the run needs
    memory it does not yet have — an answer of "it fits" is worth nothing
    without the sentence naming what has to be stopped for it to fit."""

    def as_dict(self) -> dict:
        return asdict(self)


def plan_run(
    spec: FineTuneSpec,
    gpu: GpuState | None = None,
    reclaimable_mib: int = 0,
) -> LaunchPlan:
    """Decide whether this job can start, and say why not.

    A launch is not a bare ``docker run``: it first stops the voice stack
    and evicts Ollama's loaded models, so the card a trainer wakes up on
    is emptier than the one this function is looking at. Judging against
    what is free *right now* refuses runs that would fit with room to
    spare, and the operator is left staring at a number that was never
    the relevant one. The caller that owns the arbitration passes what it
    is about to reclaim; nothing is assumed here, because a caller that
    frees nothing must still get an honest refusal.
    """
    gpu = gpu if gpu is not None else read_gpu_state()
    required = estimate_required_mib(spec)
    reclaimable = max(0, reclaimable_mib)
    effective_free = gpu.free_mib + reclaimable
    blockers = list(spec.validate())
    notes: list[str] = []
    if not gpu.available:
        blockers.append("no GPU visible to nvidia-smi")
    elif effective_free < required + DESKTOP_RESERVE_MIB:
        # The reserve sits on top of what the desktop already holds, not
        # inside it: the compositor grows when a browser tab wakes up at
        # hour three, and that growth is what has to fit.
        blockers.append(
            f"GPU has {gpu.free_mib} MiB free"
            + (f" and {reclaimable} MiB reclaimable" if reclaimable else "")
            + f"; this job needs about {required} MiB plus "
            f"{DESKTOP_RESERVE_MIB} MiB reserved for the desktop. "
            "Free VRAM or lower max_sequence_length."
        )
    elif reclaimable and gpu.free_mib < required + DESKTOP_RESERVE_MIB:
        notes.append(
            f"Fits only after launch reclaims {reclaimable} MiB: the voice "
            f"stack is stopped and Ollama's loaded models are evicted. Free "
            f"now is {gpu.free_mib} MiB, and this job needs about {required} "
            f"MiB plus {DESKTOP_RESERVE_MIB} MiB reserved for the desktop."
        )
    if not shutil.which("docker"):
        blockers.append("docker is not available on this host")
    return LaunchPlan(
        spec=spec.as_dict(),
        gpu=asdict(gpu),
        required_mib=required,
        can_run=not blockers,
        blockers=blockers,
        reclaimable_mib=reclaimable,
        notes=notes,
    )


def huggingface_cache_dir() -> str:
    """Host directory holding downloaded base weights.

    This path is handed to ``docker run``, so it is interpreted by the
    daemon on the host — not inside whatever container this code happens to
    be running in. A service container therefore has to be *told* the host
    path via ``HF_HOME``; left to guess, it would derive one from its own
    home directory and have Docker create that directory on the host.

    Creating it is best-effort for the same reason: the path may well not
    exist in this filesystem, and that is not an error.
    """
    configured = os.environ.get("HF_HOME") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    path = Path(configured) if configured else Path.home() / ".cache" / "huggingface"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return str(path)


def spec_filename(output_name: str) -> str:
    """Spec file name for this job, unique per run within a dataset dir.

    A fixed ``finetune_spec.json`` is read by the trainer minutes after
    launch — after the image pull and the base-weights download — so a
    second launch against the same dataset directory could rewrite the
    file a running trainer had not yet read, and it would train
    hyperparameters nobody selected while the ledger recorded the first
    run's options. Keying the name to the job removes the collision
    rather than racing it.
    """
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in output_name)
    return f"finetune_spec.{safe or 'job'}.json"


def write_spec(spec: FineTuneSpec, out_dir: str | Path) -> Path:
    """Persist the job spec beside the dataset it trains on."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / spec_filename(spec.output_name)
    path.write_text(json.dumps(spec.as_dict(), indent=2, sort_keys=True))
    return path


def launch(
    spec: FineTuneSpec,
    out_dir: str | Path,
    image: str = DEFAULT_TRAINER_IMAGE,
    gpu: GpuState | None = None,
) -> dict:
    """Start the trainer container detached. Returns a structured result.

    Never raises on a refusal — a blocked launch is an expected outcome
    while the GPU is busy serving inference, and the caller renders the
    blockers rather than a stack trace.
    """
    gpu = gpu if gpu is not None else read_gpu_state()
    if not spec.gpu_memory_fraction:
        spec.gpu_memory_fraction = memory_fraction_for(gpu.total_mib)
    plan = plan_run(spec, gpu=gpu)
    if not plan.can_run:
        # Deliberately before write_spec: a refused launch used to
        # overwrite the spec file of a job already training out of the
        # same dataset directory.
        return {
            "launched": False,
            "plan": plan.as_dict(),
            "spec_path": "",
        }
    spec_path = write_spec(spec, out_dir)

    host_dir = str(Path(out_dir).resolve())
    command = [
        "docker",
        "run",
        "--detach",
        # No --rm. The container is the only record of how the run ended:
        # self-removal on exit leaves nothing to read an exit code from,
        # so a crash at hour three is indistinguishable from a success and
        # gets reported as one. The watcher removes it after reading.
        "--label",
        f"hive.training.output_name={spec.output_name}",
        "--gpus",
        "all",
        "--name",
        f"hive-trainer-{spec.output_name}",
        "--volume",
        f"{host_dir}:/workspace",
        # Base weights are tens of gigabytes and every run of every model
        # would re-download them into a fresh container filesystem. The
        # cache is the difference between a run starting in seconds and a
        # run starting in an hour.
        "--volume",
        f"{huggingface_cache_dir()}:/root/.cache/huggingface",
        image,
        "--mode",
        "train",
        "--spec",
        f"/workspace/{spec_filename(spec.output_name)}",
    ]
    # Some bases are gated behind an accepted licence. A token in the
    # environment is passed through; its absence is not an error, because
    # most of the catalog needs none.
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if token:
        command[3:3] = ["--env", f"HF_TOKEN={token}"]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=120, check=True
        )
    except subprocess.CalledProcessError as exc:
        return {
            "launched": False,
            "plan": plan.as_dict(),
            "spec_path": str(spec_path),
            "error": (exc.stderr or exc.stdout or "").strip()[:2000],
        }
    except (subprocess.SubprocessError, OSError) as exc:
        return {
            "launched": False,
            "plan": plan.as_dict(),
            "spec_path": str(spec_path),
            "error": str(exc),
        }
    return {
        "launched": True,
        "container_id": completed.stdout.strip(),
        "plan": plan.as_dict(),
        "spec_path": str(spec_path),
    }


def ollama_modelfile(spec: FineTuneSpec, adapter_dir: str, base_tag: str) -> str:
    """Modelfile that serves the trained adapter through Ollama.

    Serving the adapter rather than a merged model keeps the base weights
    shared with everything else already pulled, so a new fine-tune costs
    adapter-sized disk instead of another 20 GB.
    """
    return "\n".join(
        [
            f"FROM {base_tag}",
            f"ADAPTER {adapter_dir}",
            'PARAMETER temperature 0.2',
            'PARAMETER num_ctx 32768',
        ]
    )
