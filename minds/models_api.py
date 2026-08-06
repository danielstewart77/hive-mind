"""What models *this* mind may run, answered by the mind itself.

The console needs a model picker per mind, and the honest source is the
mind's own credential. Every mind holds its own ``hmp-`` client key on the
inference proxy, and the proxy already filters its catalog by that key —
admin-only deployments are hidden from an unprivileged client. Asking the
proxy with the mind's key therefore makes the picker and the permission the
same fact: a model that does not appear is a model that mind would be
refused if it asked.

A shared catalog key cannot do that. It would list every deployment to
every mind and leave the console offering choices the proxy would reject at
the first turn, which surfaces as a mind that silently stops answering
rather than as a setting that was never allowed.

This lives beside ``runtime_api`` and ``skills_api`` for the reason those
do: a container in this stack, a bare-metal mind on this host and a mind on
another machine are one code path when the mind reports its own state.
No bind mount reaches the third one, and no central registry knows which
key the third one holds.

The proxy splits its catalog by wire format, which is exactly the split a
caller needs: a claude harness can only address the Anthropic-Messages
deployments and a codex harness only the OpenAI-shaped ones. A mind whose
provider is Ollama also reports what is pulled on its box.
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
_BASE_URL_VARS = ("INFERENCE_PROXY_URL", "ANTHROPIC_BASE_URL")
_KEY_VARS = ("MIND_PROXY_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")


def _bare_tag(name: str) -> str:
    """``gemma4-131k:latest`` and ``gemma4-131k`` are the same model."""
    return name[: -len(":latest")] if name.endswith(":latest") else name


def _first_env(names: tuple[str, ...], env: dict[str, str]) -> str:
    for name in names:
        value = str(env.get(name) or "").strip()
        if value:
            return value
    return ""


def _mind_env(path: Path) -> dict[str, str]:
    """The mind's environment, with its runtime.yaml env block layered under.

    A container gets its key from compose; a bare-metal mind gets it from
    the ``env:`` block its spawns already apply. Reading both means one
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


async def _proxy_catalog(base_url: str, key: str) -> list[tuple[str, str]]:
    """``(name, provider)`` for every deployment this key may address."""
    if not base_url or not key:
        return []
    root = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {key}"}
    found: list[tuple[str, str]] = []
    async with aiohttp.ClientSession(headers=headers) as session:
        for path, provider in (
            ("/v1/anthropic/models", "anthropic"),
            ("/v1/models", "openai"),
        ):
            try:
                async with session.get(f"{root}{path}", timeout=_TIMEOUT) as resp:
                    if resp.status != 200:
                        continue
                    payload = await resp.json()
            except Exception:
                # One endpoint failing must not discard the other's answer.
                continue
            for row in payload.get("data", []):
                name = str(row.get("id") or "")
                if name:
                    found.append((name, provider))
    return found


async def _ollama_catalog(api_base: str) -> list[str]:
    if not api_base:
        return []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{api_base.rstrip('/')}/api/tags", timeout=_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    return []
                payload = await resp.json()
    except Exception:
        return []
    return [str(m.get("name")) for m in payload.get("models", []) if m.get("name")]


async def build_catalog(path: Path, *, static_aliases: dict[str, str]) -> list[dict]:
    """Every model this mind can be pointed at, tagged by how it is reached.

    Ollama is read before the proxy so a proxy row whose upstream is the
    local Ollama — which is how a fine-tune gets metered — is recognised as
    the Ollama model it is rather than listed twice under two providers.
    """
    runtime = load_runtime(path)
    env = _mind_env(path)
    result: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(name: str, provider: str) -> None:
        if name and (name, provider) not in seen:
            seen.add((name, provider))
            result.append({"name": name, "provider": provider})

    for alias, provider in static_aliases.items():
        add(alias, provider)

    ollama_names: set[str] = set()
    if str(runtime.get("provider") or "") == "ollama":
        api_base = _first_env(("OLLAMA_BASE_URL", "OLLAMA_HOST"), env)
        # Listed by bare name: Ollama reports an untagged pull as
        # `name:latest`, both forms resolve to the same model, and the bare
        # one is what a mind's `default_model` is written as — so offering
        # the tagged form would leave a mind's own model missing from its
        # own picker.
        ollama_names = {_bare_tag(n) for n in await _ollama_catalog(api_base)}
        for name in sorted(ollama_names):
            add(name, "ollama")

    local = ollama_names

    for name, provider in await _proxy_catalog(
        _first_env(_BASE_URL_VARS, env), _first_env(_KEY_VARS, env)
    ):
        if provider == "openai" and _bare_tag(name) in local:
            continue
        add(name, provider)

    return result


def install_models_route(
    app: FastAPI,
    *,
    path: Path,
    mind_id: str,
    log,
    static_aliases: dict[str, str] | None = None,
) -> None:
    """Admin-guarded, like every other configuration route on a mind.

    The listing names this mind's reachable deployments, which is a map of
    what its credential unlocks — not something to hand out on a port that
    answers across the LAN.
    """
    aliases = static_aliases or {"opus": "anthropic", "sonnet": "anthropic", "haiku": "anthropic"}

    @app.get("/models")
    async def get_models(request: Request) -> Any:
        refusal = authorize_admin(request)
        if refusal is not None:
            return refusal
        try:
            models = await build_catalog(path, static_aliases=aliases)
        except Exception as exc:  # noqa: BLE001
            log_event(
                log, "mind.models.failed", level=logging.WARNING,
                mind_id=mind_id, error=str(exc),
            )
            return JSONResponse({"error": str(exc)}, status_code=502)
        return {"models": models}
