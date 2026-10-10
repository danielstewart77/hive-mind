"""What models *this* mind may run, answered by the mind itself.

The console needs a model picker per mind, and the honest source is the mind's
own credential. Every mind holds its own ``hmp-`` client key on the inference
proxy, and the proxy filters its listing by that key and by the harness the
listing was asked on — so a model that does not appear here is a model this
mind would be refused if it asked. The picker and the permission are the same
fact.

The listing is relayed, not assembled. The proxy owns the providers table, so
each row already names the upstream hosting it, and a provider added there
becomes selectable here with no code change. A mind that invented its own
provider labels, or pasted in a house list of short aliases, would be offering
choices nothing downstream honours.

Which models a listing carries is decided by the harness and nothing else, and
the harness is named on the request (`?harness=claude`, `codex` or `dsh`). A
mind runs all three, so the caller names the conversation's harness; the
file's default harness answers only when it names none.

This lives beside ``runtime_api`` and ``skills_api`` for the reason those do: a
container in this stack, a bare-metal mind on this host and a mind on another
machine are one code path when the mind reports its own state. No bind mount
reaches the third one, and no central registry knows which key it holds.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import aiohttp
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core.hive_logging import log_event
from minds.runtime_api import authorize_admin, load_runtime

_TIMEOUT = aiohttp.ClientTimeout(total=6)

# Order matters: an explicit proxy URL wins over the harness variable that
# happens to point at the same place, because a codex mind has no
# ANTHROPIC_BASE_URL at all — it carries its provider in CODEX_HOME's
# config.toml, which is not readable as an environment variable.
_BASE_URL_VARS = ("INFERENCE_PROXY_URL", "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL")
_KEY_VARS = ("MIND_PROXY_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")

#: One listing, filtered by the proxy to what the named harness can send:
#: claude gets Anthropic and Ollama, codex gets OpenAI and Ollama, dsh gets
#: everything. Which models a mind may run is the harness's limit, not the
#: mind's — and unnamed, the proxy returns the union, offering models the
#: harness cannot speak to.
_LISTING_PATH = "/v1/models"


def _harness_family(harness: str) -> str:
    name = str(harness or "")
    return name[: -len("_cli")] if name.endswith("_cli") else name


def _proxy_root(base_url: str) -> str:
    """The proxy's root, given whatever a harness variable happens to hold.

    The listing paths above are absolute from the root, while the variables
    these are read from are the ones a client SDK is handed — and an OpenAI
    SDK's base URL includes the API prefix, so ``OPENAI_BASE_URL`` ends in
    ``/v1``. Appending a listing path to it addresses ``/v1/v1/models``, which
    the proxy answers 404, which this module reports as an empty list — and an
    empty list is how the console says "this mind is offered nothing", a
    sentence the operator acts on by editing credentials that were never wrong.
    """
    root = base_url.rstrip("/")
    return root[: -len("/v1")] if root.endswith("/v1") else root


def _first_env(names: tuple[str, ...], env: dict[str, str]) -> str:
    for name in names:
        value = str(env.get(name) or "").strip()
        if value:
            return value
    return ""


def _mind_env(path: Path) -> dict[str, str]:
    """The mind's environment, with its runtime.yaml env block layered under.

    A container gets its key from compose; a bare-metal mind gets it from the
    ``env:`` block its spawns already apply. Reading both means one
    implementation answers for both deployments.
    """
    merged: dict[str, str] = {}
    try:
        declared = load_runtime(path).get("env") or {}
        if isinstance(declared, dict):
            merged.update({str(k): str(v) for k, v in declared.items()})
    except Exception:
        pass
    merged.update({k: v for k, v in os.environ.items()})
    return merged


async def build_catalog(path: Path, harness: str | None = None) -> list[dict]:
    """Every model this mind may be pointed at, as the proxy reports it.

    Each row carries the deployment name that gets written to configuration,
    the label a picker displays, and the provider hosting it. An unreachable
    proxy yields an empty list rather than raising: the console distinguishes
    "nothing offered" from "the mind is down", and the two need different words
    on screen.

    ``harness`` names whose listing is wanted. A mind runs every harness, and
    a conversation's models are its own harness's, not the default's — so the
    file's `harness` is only the answer when the caller names none.
    """
    runtime = load_runtime(path)
    env = _mind_env(path)
    base_url = _first_env(_BASE_URL_VARS, env)
    key = _first_env(_KEY_VARS, env)
    if not base_url or not key:
        return []
    family = _harness_family(str(harness or runtime.get("harness") or ""))
    url = f"{_proxy_root(base_url)}{_LISTING_PATH}?harness={family}"
    try:
        async with aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {key}"}
        ) as session:
            async with session.get(url, timeout=_TIMEOUT) as resp:
                if resp.status != 200:
                    return []
                payload = await resp.json()
    except Exception:
        return []

    rows: list[dict] = []
    seen: set[str] = set()
    for row in payload.get("data", []):
        name = str(row.get("id") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        provider = str(row.get("provider") or row.get("owned_by") or "")
        rows.append(
            {
                "name": name,
                "label": str(row.get("label") or name),
                "provider": provider,
                "provider_label": str(row.get("provider_label") or provider),
                # The window belongs to the model, so the proxy is the only
                # thing that knows it. Relayed rather than mapped here, and
                # left absent when nobody has declared it: a zero would make
                # every conversation render as infinitely full.
                "context_window": (
                    row.get("context_window")
                    if isinstance(row.get("context_window"), int)
                    else None
                ),
                # The levels a picker may offer, in the proxy's order. Empty
                # when the model takes no effort setting, or when an older
                # proxy says nothing about it.
                "effort_levels": [
                    str(level) for level in (row.get("effort_levels") or [])
                    if isinstance(level, str) and level
                ] if isinstance(row.get("effort_levels"), list) else [],
            }
        )
    return rows


async def context_window(path: Path, harness: str, model: str) -> int | None:
    """The window the proxy declares for one model on one harness, or None.

    None for a model nobody has measured, an unreachable proxy, or a model the
    harness does not offer: each is "no window known", and a guessed one is a
    rotation threshold sized for room the conversation does not have.
    """
    for row in await build_catalog(path, harness=harness):
        if row.get("name") == model:
            window = row.get("context_window")
            return window if isinstance(window, int) and window > 0 else None
    return None


def install_models_route(
    app: FastAPI,
    *,
    path: Path,
    mind_id: str,
    log,
) -> None:
    """Admin-guarded, like every other configuration route on a mind.

    The listing names this mind's reachable deployments, which is a map of what
    its credential unlocks — not something to hand out on a port that answers
    across the LAN.
    """

    @app.get("/models")
    async def get_models(request: Request, harness: str = "") -> Any:
        refusal = authorize_admin(request)
        if refusal is not None:
            return refusal
        try:
            models = await build_catalog(path, harness=harness or None)
        except Exception as exc:  # noqa: BLE001
            log_event(
                log, "mind.models.failed", level=logging.WARNING,
                mind_id=mind_id, error=str(exc),
            )
            return JSONResponse({"error": str(exc)}, status_code=502)
        return {"models": models}
