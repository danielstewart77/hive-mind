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

    def validate(self) -> list[str]:
        """Return human-readable problems; empty means the spec is usable."""
        problems: list[str] = []
        if not self.train_file or not Path(self.train_file).exists():
            problems.append(f"train file not found: {self.train_file!r}")
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

    def as_dict(self) -> dict:
        return asdict(self)


def plan_run(spec: FineTuneSpec, gpu: GpuState | None = None) -> LaunchPlan:
    """Decide whether this job can start right now, and say why not."""
    gpu = gpu if gpu is not None else read_gpu_state()
    required = estimate_required_mib(spec)
    blockers = list(spec.validate())
    if not gpu.available:
        blockers.append("no GPU visible to nvidia-smi")
    elif gpu.free_mib < required:
        blockers.append(
            f"GPU has {gpu.free_mib} MiB free; this job needs about "
            f"{required} MiB. Free VRAM or lower max_sequence_length."
        )
    if not shutil.which("docker"):
        blockers.append("docker is not available on this host")
    return LaunchPlan(
        spec=spec.as_dict(),
        gpu=asdict(gpu),
        required_mib=required,
        can_run=not blockers,
        blockers=blockers,
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


def write_spec(spec: FineTuneSpec, out_dir: str | Path) -> Path:
    """Persist the job spec beside the dataset it trains on."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "finetune_spec.json"
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
    plan = plan_run(spec, gpu=gpu)
    spec_path = write_spec(spec, out_dir)
    if not plan.can_run:
        return {
            "launched": False,
            "plan": plan.as_dict(),
            "spec_path": str(spec_path),
        }

    host_dir = str(Path(out_dir).resolve())
    command = [
        "docker",
        "run",
        "--rm",
        "--detach",
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
        "/workspace/finetune_spec.json",
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
