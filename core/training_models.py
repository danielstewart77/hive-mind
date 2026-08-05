"""Which base models a fine-tune can start from, and what each one costs.

The base model is the single most consequential choice in a fine-tune and
the least legible one: the names carry a parameter count and nothing else,
while the properties that decide whether a run finishes overnight or dies
at hour six — dense or mixture-of-experts, whether the served copy is the
same weights that were trained, how much VRAM a LoRA over it actually needs
— are nowhere in the name.

So the catalog is data, not a dropdown hardcoded in a template. Each entry
carries the training repo, the Ollama tag the adapter will be served over,
and a plain-language note about what the trade is. The console renders the
notes as help text; the CLI prints the same rows.

Availability is resolved at call time against the local Ollama, because a
model you have not pulled is a forty-minute download standing between a
click and a served adapter, and that is worth saying up front rather than
discovering at deploy.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")

ARCH_DENSE = "dense"
ARCH_MOE = "moe"

#: The sequence length the catalog quotes its VRAM figures at. The train
#: form defaults here too, so a number shown beside a model is the number
#: that model will actually need if nothing else is touched.
REFERENCE_SEQUENCE_LENGTH = 8_192


@dataclass(frozen=True)
class BaseModel:
    """One candidate base, with the facts that decide whether to pick it."""

    id: str
    """HuggingFace repo the trainer loads weights from."""

    label: str
    parameters_b: float
    architecture: str
    serve_tag: str
    """Ollama tag naming this base. Must be the same weights the trainer
    loaded, quantized."""

    note: str
    """Why you would pick this one, in a sentence a non-specialist reads."""

    recommended: bool = False
    weights_vram_mib: int = 0
    """What the weights alone occupy under 4-bit loading. Crude on purpose:
    it is the model-dependent half of the launch planner's estimate, and
    exists to stop a run that will certainly OOM rather than to predict
    allocation. Activations — the sequence-length-dependent half — are the
    planner's to add, since they depend on settings this entry knows
    nothing about."""

    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def train_vram_mib(self) -> int:
        """What a run at the default 8k sequence length needs, for display."""
        return self.weights_vram_mib + int(REFERENCE_SEQUENCE_LENGTH * 1.6)

    def as_dict(self) -> dict:
        return {**asdict(self), "train_vram_mib": self.train_vram_mib}


# Ordered smallest-first within each architecture group. Every entry is a
# model whose weights are openly downloadable and whose architecture peft
# supports without a custom target-module map.
CATALOG: tuple[BaseModel, ...] = (
    BaseModel(
        id="Qwen/Qwen3-4B-Instruct-2507",
        label="Qwen3 4B Instruct",
        parameters_b=4.0,
        architecture=ARCH_DENSE,
        serve_tag="qwen3:4b",
        note=(
            "The fastest way to find out whether the corpus teaches anything "
            "at all. A run finishes in under an hour, so a bad dataset costs "
            "an evening rather than a weekend. Too small to drive a harness "
            "well on its own — treat a good result here as a green light for "
            "a larger base, not as the finished product."
        ),
        weights_vram_mib=4_000,
        tags=("fast", "smoke-test"),
    ),
    BaseModel(
        id="Qwen/Qwen3-8B",
        label="Qwen3 8B",
        parameters_b=8.0,
        architecture=ARCH_DENSE,
        serve_tag="qwen3:8b",
        note=(
            "The honest starting point. Dense, so LoRA behaves predictably "
            "and the loss curve means what it looks like it means; small "
            "enough to train in a few hours while the A6000 keeps serving "
            "inference; large enough to hold a tool-calling format and a "
            "house style at the same time."
        ),
        recommended=True,
        weights_vram_mib=6_500,
        tags=("balanced", "first-real-run"),
    ),
    BaseModel(
        id="mistralai/Mistral-Nemo-Instruct-2407",
        label="Mistral Nemo 12B",
        parameters_b=12.2,
        architecture=ARCH_DENSE,
        serve_tag="mistral-nemo:latest",
        note=(
            "A dense 12B with a long native context and a different lineage "
            "from the Qwen family. Worth a run if a Qwen fine-tune comes out "
            "stylistically flat — a second opinion from a different pretrain "
            "is cheaper than another epoch on the same one."
        ),
        weights_vram_mib=9_000,
        tags=("alternative-lineage",),
    ),
    BaseModel(
        id="Qwen/Qwen3-14B",
        label="Qwen3 14B",
        parameters_b=14.8,
        architecture=ARCH_DENSE,
        serve_tag="qwen3:14b",
        note=(
            "The largest dense model that still trains comfortably beside a "
            "live inference workload. Pick it once an 8B run has proved the "
            "dataset is worth the extra hours."
        ),
        weights_vram_mib=10_500,
        tags=("balanced",),
    ),
    BaseModel(
        id="openai/gpt-oss-20b",
        label="GPT-OSS 20B",
        parameters_b=20.9,
        architecture=ARCH_MOE,
        serve_tag="gpt-oss:20b",
        note=(
            "Mixture-of-experts: only a fraction of the weights fire per "
            "token, so it is fast to serve for its size. That same routing "
            "makes fine-tuning fussier — a LoRA can end up training the few "
            "experts your corpus happens to activate and leaving the rest "
            "untouched. Already the default for several minds here."
        ),
        weights_vram_mib=14_000,
        tags=("moe", "already-served"),
    ),
    BaseModel(
        id="Qwen/Qwen3-Coder-30B-A3B-Instruct",
        label="Qwen3 Coder 30B (A3B)",
        parameters_b=30.5,
        architecture=ARCH_MOE,
        serve_tag="qwen3-coder:latest",
        note=(
            "Pretrained on code and already good at the job this corpus "
            "teaches, which cuts both ways: less to learn, and less headroom "
            "for the fine-tune to show an improvement. Mixture-of-experts, "
            "so expect a longer run and a fussier result than a dense base."
        ),
        weights_vram_mib=19_500,
        tags=("moe", "code"),
    ),
    BaseModel(
        id="Qwen/Qwen3-30B-A3B-Instruct-2507",
        label="Qwen3 30B Instruct (A3B)",
        parameters_b=30.5,
        architecture=ARCH_MOE,
        serve_tag="qwen3:30b-a3b-instruct-2507-q4_K_M",
        note=(
            "The largest base this hardware can train at all, and the one "
            "the pipeline shipped with as a placeholder default. A run takes "
            "most of a day and needs the GPU to itself, and it is a "
            "mixture-of-experts model, so only a fraction of its experts see "
            "your corpus and the result is harder to predict than a dense "
            "base of half the size. Reach for it when a smaller fine-tune "
            "has already proved out the dataset."
        ),
        weights_vram_mib=19_800,
        tags=("moe", "largest"),
    ),
)

BY_ID = {model.id: model for model in CATALOG}
DEFAULT_BASE_MODEL_ID = next(m.id for m in CATALOG if m.recommended)


def get(model_id: str) -> BaseModel | None:
    return BY_ID.get(model_id)


def installed_serve_tags(ollama_url: str | None = None, timeout: int = 5) -> set[str]:
    """Ollama tags present locally. An unreachable Ollama is an empty set.

    Deploy is what actually needs the tag, so a failure to ask is reported
    as "unknown" rather than as an error that blocks planning a run.
    """
    url = (ollama_url or OLLAMA_URL).rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.load(response)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return set()
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return set()
    return {m.get("name", "") for m in models if isinstance(m, dict)}


def catalog(
    free_vram_mib: int | None = None, ollama_url: str | None = None
) -> list[dict]:
    """The catalog as dicts, annotated with what this host can do right now.

    ``serve_tag_installed`` answers "can I deploy this without a download",
    ``fits_now`` answers "can I train it without freeing the GPU first".
    Both are advisory: the launch planner is what actually refuses a run.
    """
    installed = installed_serve_tags(ollama_url)
    rows = []
    for model in CATALOG:
        row = model.as_dict()
        row["serve_tag_installed"] = model.serve_tag in installed
        row["fits_now"] = (
            None if free_vram_mib is None else free_vram_mib >= model.train_vram_mib
        )
        rows.append(row)
    return rows
