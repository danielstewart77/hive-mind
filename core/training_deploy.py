"""Turn a trained adapter into a model Ollama will serve.

Training produces a LoRA adapter: a few hundred megabytes of weight deltas
that mean nothing on their own. Deploy is what puts them in front of a
mind, and there are two honest ways to do it.

**adapter** stacks the adapter on top of a base tag already pulled. Ollama
holds one copy of the base for every fine-tune built on it, so a deploy
costs adapter-sized disk and finishes in seconds. The catch is that the
adapter was fitted against the full-precision weights while the served tag
is a quantized copy of them — close, not identical, and the difference
shows up as a fine-tune that feels weaker than its eval score.

**merge** folds the adapter into the weights, converts the result to GGUF
and quantizes that. No mismatch, because the thing being quantized is the
thing that was trained. It costs a conversion pass and a full model's worth
of disk per deploy.

Merging needs torch and a converter, which is exactly the dependency set
kept out of every service image, so it runs in the trainer container the
same way training does. This module therefore has two shapes of return: a
deploy that finished, and a conversion that started and will finish later.

Everything here talks to Ollama over HTTP rather than shelling out to the
CLI, because the caller is usually a container that has an Ollama endpoint
and no Ollama binary.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")

STRATEGY_ADAPTER = "adapter"
STRATEGY_MERGE = "merge"
STRATEGIES = (STRATEGY_ADAPTER, STRATEGY_MERGE)

# What the trainer writes, and what a merge run is expected to produce.
ADAPTER_WEIGHT_NAMES = ("adapter_model.safetensors", "adapter_model.bin")
MERGED_GGUF_NAME = "merged.gguf"

DEFAULT_QUANTIZATION = "q4_K_M"
DEFAULT_TEMPERATURE = 0.2
DEFAULT_NUM_CTX = 32_768


@dataclass
class DeployRequest:
    """One deploy, fully described.

    ``serve_tag`` is the base the adapter rides on under the adapter
    strategy and the tokenizer/config source under merge. It is not a free
    choice: it must be the same model the adapter was trained against, so
    the caller passes the base model's catalog entry rather than inventing
    one.
    """

    model_name: str
    adapter_dir: str
    serve_tag: str
    strategy: str = STRATEGY_ADAPTER
    base_model: str = ""
    quantization: str = DEFAULT_QUANTIZATION
    temperature: float = DEFAULT_TEMPERATURE
    num_ctx: int = DEFAULT_NUM_CTX
    system_prompt: str = ""

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.model_name:
            problems.append("model_name is required")
        if self.strategy not in STRATEGIES:
            problems.append(
                f"unknown strategy {self.strategy!r}; expected one of "
                + ", ".join(STRATEGIES)
            )
        if not self.serve_tag:
            problems.append("serve_tag is required")
        adapter = Path(self.adapter_dir)
        if not adapter.is_dir():
            problems.append(f"adapter directory not found: {self.adapter_dir!r}")
        elif self.strategy == STRATEGY_ADAPTER and adapter_weight_file(adapter) is None:
            problems.append(
                f"no adapter weights in {self.adapter_dir!r} — expected one of "
                + ", ".join(ADAPTER_WEIGHT_NAMES)
            )
        if self.num_ctx < 512:
            problems.append("num_ctx must be at least 512")
        return problems

    def as_dict(self) -> dict:
        return {
            "model_name": self.model_name,
            "adapter_dir": self.adapter_dir,
            "serve_tag": self.serve_tag,
            "strategy": self.strategy,
            "base_model": self.base_model,
            "quantization": self.quantization,
            "temperature": self.temperature,
            "num_ctx": self.num_ctx,
            "system_prompt": self.system_prompt,
        }


@dataclass
class DeployResult:
    deployed: bool = False
    model_name: str = ""
    strategy: str = ""
    stage: str = ""
    """``served`` when Ollama has the model, ``converting`` when a merge
    container is still working, ``refused`` when nothing was started."""

    detail: str = ""
    blockers: list[str] = field(default_factory=list)
    container_id: str = ""
    modelfile: str = ""

    def as_dict(self) -> dict:
        return {
            "deployed": self.deployed,
            "model_name": self.model_name,
            "strategy": self.strategy,
            "stage": self.stage,
            "detail": self.detail,
            "blockers": list(self.blockers),
            "container_id": self.container_id,
            "modelfile": self.modelfile,
        }


def adapter_weight_file(adapter_dir: str | Path) -> Path | None:
    """The adapter's weight file, whichever name the trainer wrote."""
    directory = Path(adapter_dir)
    for name in ADAPTER_WEIGHT_NAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def render_modelfile(request: DeployRequest, from_ref: str) -> str:
    """The Modelfile equivalent of this deploy, for display and for record.

    Ollama's HTTP API takes structured fields rather than a Modelfile, but
    the Modelfile is the form anyone debugging this will recognize, so it is
    rendered, returned and stored beside the adapter.
    """
    lines = [f"FROM {from_ref}"]
    if request.strategy == STRATEGY_ADAPTER:
        lines.append(f"ADAPTER {request.adapter_dir}")
    lines.append(f"PARAMETER temperature {request.temperature}")
    lines.append(f"PARAMETER num_ctx {request.num_ctx}")
    if request.system_prompt:
        lines.append(f'SYSTEM """{request.system_prompt}"""')
    return "\n".join(lines) + "\n"


def _post_json(url: str, payload: dict, timeout: int) -> dict:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read().decode(errors="replace").strip()
    if not text:
        return {}
    # /api/create streams newline-delimited progress even when stream is
    # false on older builds; the last object is the outcome.
    last = text.splitlines()[-1]
    try:
        return json.loads(last)
    except json.JSONDecodeError:
        return {"status": last[:500]}


def upload_blob(path: Path, ollama_url: str, timeout: int = 900) -> str:
    """Push a file to Ollama's blob store and return its ``sha256:`` digest.

    Ollama addresses adapter and model files by digest, not by path — it
    has no reason to trust that a path visible to the caller is visible to
    the server. Re-uploading an identical blob is a no-op on the server, so
    this is safe to call on every deploy.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    reference = f"sha256:{digest.hexdigest()}"
    url = f"{ollama_url.rstrip('/')}/api/blobs/{reference}"

    head = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(head, timeout=30):
            return reference
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    except urllib.error.URLError:
        pass

    with path.open("rb") as handle:
        push = urllib.request.Request(
            url,
            data=handle,
            method="POST",
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(path.stat().st_size),
            },
        )
        urllib.request.urlopen(push, timeout=timeout)
    return reference


def deploy(
    request: DeployRequest,
    ollama_url: str | None = None,
    trainer_image: str = "hive-mind-trainer:latest",
    workspace: str | Path | None = None,
) -> DeployResult:
    """Serve this adapter, or start the conversion that will let us.

    Never raises on a refusal. A missing adapter, an unreachable Ollama and
    a merge that has not been converted yet are all expected states with
    different next actions, and the caller renders them.
    """
    ollama_url = (ollama_url or OLLAMA_URL).rstrip("/")
    problems = request.validate()
    if problems:
        return DeployResult(
            model_name=request.model_name,
            strategy=request.strategy,
            stage="refused",
            blockers=problems,
        )

    if request.strategy == STRATEGY_MERGE:
        return _deploy_merged(request, ollama_url, trainer_image, workspace)
    return _deploy_adapter(request, ollama_url)


def _create_model(
    request: DeployRequest, ollama_url: str, payload: dict, from_ref: str
) -> DeployResult:
    modelfile = render_modelfile(request, from_ref)
    try:
        response = _post_json(f"{ollama_url}/api/create", payload, timeout=3_600)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return DeployResult(
            model_name=request.model_name,
            strategy=request.strategy,
            stage="refused",
            blockers=[f"Ollama at {ollama_url} refused the create: {exc}"],
            modelfile=modelfile,
        )
    status = str(response.get("status") or response.get("error") or "")
    if response.get("error"):
        return DeployResult(
            model_name=request.model_name,
            strategy=request.strategy,
            stage="refused",
            blockers=[status],
            modelfile=modelfile,
        )
    return DeployResult(
        deployed=True,
        model_name=request.model_name,
        strategy=request.strategy,
        stage="served",
        detail=status or "created",
        modelfile=modelfile,
    )


def _deploy_adapter(request: DeployRequest, ollama_url: str) -> DeployResult:
    weights = adapter_weight_file(request.adapter_dir)
    assert weights is not None  # validate() already established this
    try:
        digest = upload_blob(weights, ollama_url)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return DeployResult(
            model_name=request.model_name,
            strategy=request.strategy,
            stage="refused",
            blockers=[f"could not upload the adapter to {ollama_url}: {exc}"],
        )
    payload = {
        "model": request.model_name,
        "from": request.serve_tag,
        "adapters": {weights.name: digest},
        "parameters": {
            "temperature": request.temperature,
            "num_ctx": request.num_ctx,
        },
        "stream": False,
    }
    if request.system_prompt:
        payload["system"] = request.system_prompt
    return _create_model(request, ollama_url, payload, request.serve_tag)


def merged_gguf_path(adapter_dir: str | Path) -> Path:
    return Path(adapter_dir).parent / MERGED_GGUF_NAME


def _deploy_merged(
    request: DeployRequest,
    ollama_url: str,
    trainer_image: str,
    workspace: str | Path | None,
) -> DeployResult:
    gguf = merged_gguf_path(request.adapter_dir)
    if gguf.is_file():
        try:
            digest = upload_blob(gguf, ollama_url)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return DeployResult(
                model_name=request.model_name,
                strategy=request.strategy,
                stage="refused",
                blockers=[f"could not upload the merged model: {exc}"],
            )
        payload = {
            "model": request.model_name,
            "files": {gguf.name: digest},
            "parameters": {
                "temperature": request.temperature,
                "num_ctx": request.num_ctx,
            },
            "stream": False,
        }
        if request.system_prompt:
            payload["system"] = request.system_prompt
        return _create_model(request, ollama_url, payload, gguf.name)

    return _launch_merge(request, trainer_image, workspace or gguf.parent)


def _launch_merge(
    request: DeployRequest, trainer_image: str, workspace: str | Path
) -> DeployResult:
    """Start the trainer container in merge mode. Detached, like training.

    A merge loads the base model in full precision and writes a whole model
    back out, so it is minutes-to-hours of work and gets the same treatment
    a training run does: launched, recorded, and polled later.
    """
    host_dir = str(Path(workspace).resolve())
    adapter_name = Path(request.adapter_dir).name
    command = [
        "docker",
        "run",
        "--rm",
        "--detach",
        "--gpus",
        "all",
        "--name",
        f"hive-merge-{request.model_name}".replace(":", "-"),
        "--volume",
        f"{host_dir}:/workspace",
        trainer_image,
        "--mode",
        "merge",
        "--base-model",
        request.base_model or request.serve_tag,
        "--adapter",
        f"/workspace/{adapter_name}",
        "--out",
        f"/workspace/{MERGED_GGUF_NAME}",
        "--quantization",
        request.quantization,
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=120, check=True
        )
    except subprocess.CalledProcessError as exc:
        return DeployResult(
            model_name=request.model_name,
            strategy=request.strategy,
            stage="refused",
            blockers=[(exc.stderr or exc.stdout or "docker refused the merge").strip()[:2000]],
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return DeployResult(
            model_name=request.model_name,
            strategy=request.strategy,
            stage="refused",
            blockers=[str(exc)],
        )
    return DeployResult(
        model_name=request.model_name,
        strategy=request.strategy,
        stage="converting",
        container_id=completed.stdout.strip(),
        detail=(
            "Merging the adapter into the base weights and converting to "
            f"GGUF at {request.quantization}. Run deploy again when it "
            "finishes to serve the result."
        ),
    )
