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

Which endpoint carries the listing is decided by the harness and nothing else:
a claude CLI speaks Anthropic Messages and a codex CLI the Responses shape, so
the endpoint a listing is requested on is what tells the proxy who is asking.

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

#: The listing endpoint each harness may address. A claude CLI can only speak
#: Anthropic Messages; everything else speaks the OpenAI shape.
_LISTING_PATH = {"claude": "/v1/anthropic/models", "codex": "/v1/models"}


def _harness_family(harness: str) -> str:
    return "claude" if str(harness or "").startswith("claude") else "codex"


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


async def build_catalog(path: Path) -> list[dict]:
    """Every model this mind may be pointed at, as the proxy reports it.

    Each row carries the deployment name that gets written to configuration,
    the label a picker displays, and the provider hosting it. An unreachable
    proxy yields an empty list rather than raising: the console distinguishes
    "nothing offered" from "the mind is down", and the two need different words
    on screen.
    """
    runtime = load_runtime(path)
    env = _mind_env(path)
    base_url = _first_env(_BASE_URL_VARS, env)
    key = _first_env(_KEY_VARS, env)
    if not base_url or not key:
        return []
    listing = _LISTING_PATH[_harness_family(str(runtime.get("harness") or ""))]
    url = f"{base_url.rstrip('/')}{listing}"
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
            }
        )
    return rows


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
    async def get_models(request: Request) -> Any:
        refusal = authorize_admin(request)
        if refusal is not None:
            return refusal
        try:
            models = await build_catalog(path)
        except Exception as exc:  # noqa: BLE001
            log_event(
                log, "mind.models.failed", level=logging.WARNING,
                mind_id=mind_id, error=str(exc),
            )
            return JSONResponse({"error": str(exc)}, status_code=502)
        return {"models": models}
