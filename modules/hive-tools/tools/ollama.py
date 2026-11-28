"""Generic-LLM router for hive-tools, answered by the hive's inference proxy.

Three endpoints, model-agnostic. Their paths still say `ollama` because every
mind's capture hook and remember skill calls them by that name; what answers
them is the inference proxy named by `INFERENCE_PROXY_URL`, authenticated
with the calling mind's own key — the same path every other mind and service
takes. Nothing here calls Ollama, and there is no fallback to it.

- POST /ollama/structured — structured output. The caller's JSON schema goes
  to the proxy as an OpenAI `response_format` of type `json_schema`; the
  answer is parsed and returned as the object itself.

- POST /ollama/chat — conversational text output. Returns the model's text
  verbatim.

- GET /ollama/models — the chat models the proxy offers this key.

A call that names no model gets `HIVE_TOOLS_DEFAULT_MODEL`. An unset proxy
address, key or default model is a 503 naming the variable, never a guess;
so is a refused credential. Any other proxy failure is a 502 carrying
what the proxy said. HITL gating is per-endpoint.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth_api import require_api_token
from hitl import hitl_gate

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ollama", tags=["ollama"])

DEFAULT_MODEL_VAR = "HIVE_TOOLS_DEFAULT_MODEL"
TIMEOUT_SECONDS = 90


class StructuredRequest(BaseModel):
    prompt: str
    json_schema: dict[str, Any] = Field(alias="schema")
    model: str | None = None
    system: str | None = None

    model_config = {"populate_by_name": True}


class ChatRequest(BaseModel):
    prompt: str
    model: str | None = None
    system: str | None = None


def _unset(*names: str) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=f"hive-tools cannot reach a model: {', '.join(names)} unset",
    )


def _proxy() -> str:
    """The proxy's root, read when the call is made.

    A trailing `/v1` on the address is dropped so either spelling addresses
    `/v1/...` once. The tools hold no proxy key of their own: every call
    carries the key of the mind that made it (`_calling_key`).
    """
    root = os.environ.get("INFERENCE_PROXY_URL", "").strip().rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    if not root:
        raise _unset("INFERENCE_PROXY_URL")
    return root


def _calling_key(request: Request) -> str:
    """The calling mind's own key, which is its proxy key. A call
    with no mind's key has nothing to send: refused, never a key of the
    tools' own."""
    header = request.headers.get("Authorization", "")
    key = header[7:].strip() if header.startswith("Bearer ") else ""
    if not key:
        raise HTTPException(
            status_code=503,
            detail="this call carries no calling mind's key, so the proxy was not asked")
    return key


def _model(asked: str | None) -> str:
    if asked:
        return asked
    model = os.environ.get(DEFAULT_MODEL_VAR, "").strip()
    if not model:
        raise _unset(DEFAULT_MODEL_VAR)
    return model


def _refused(resp: httpx.Response) -> HTTPException:
    """What a proxy refusal becomes: a refused credential is configuration, the rest failures.

    The proxy passes an upstream provider's own 401 through, so a refusal is
    not necessarily hive-tools' key; the detail carries the proxy's words.
    """
    detail = resp.text[:500]
    if resp.status_code in (401, 403):
        return HTTPException(
            status_code=503,
            detail=f"a credential was refused ({resp.status_code}): the calling mind's key, "
                   f"or the proxy's key for the model's provider: {detail}",
        )
    return HTTPException(
        status_code=502,
        detail=f"inference proxy answered {resp.status_code}: {detail}",
    )


async def _send(method: str, path: str, key: str, *, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
    root = _proxy()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            resp = await client.request(
                method, f"{root}{path}", json=json_body,
                headers={"Authorization": f"Bearer {key}"},
            )
    except httpx.HTTPError as e:
        log.exception("inference proxy call failed")
        raise HTTPException(status_code=502, detail=f"inference proxy unreachable: {e!r}") from e
    if resp.status_code >= 400:
        raise _refused(resp)
    try:
        return resp.json()
    except ValueError as e:
        raise HTTPException(
            status_code=502, detail=f"inference proxy answered non-JSON: {resp.text[:300]}"
        ) from e


async def _complete(
    key: str, *, model: str, prompt: str, system: str | None, schema: dict[str, Any] | None
) -> str:
    """One chat completion through the proxy; the answer's text, or "" when it has none."""
    messages: list[dict[str, str]] = []
    if system is not None:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
    if schema is not None:
        # Not strict: the schema is the caller's, and a strict upstream
        # rejects one that does not close every object.
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "response", "strict": False, "schema": schema},
        }
    payload = await _send("POST", "/v1/chat/completions", key, json_body=body)
    try:
        content = payload["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError, AttributeError):
        content = None
    return content if isinstance(content, str) else ""


@router.post("/structured")
async def structured(
    request: Request,
    body: StructuredRequest,
    caller: str = Depends(hitl_gate("ollama_structured")),
) -> dict[str, Any]:
    """Schema-constrained generation. Returns the parsed JSON object directly.

    On output that is not a JSON object — empty, prose, or a bare value —
    returns the raw string under `_raw` with an `_error` marker, which is
    also what HITL reads to tell a failed call from a successful one.
    """
    del caller
    _proxy()  # an unset proxy is named before an unset default model
    raw = await _complete(
        _calling_key(request), model=_model(body.model), prompt=body.prompt, system=body.system, schema=body.json_schema
    )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    log.warning("structured response was not a JSON object: %r", raw[:300])
    return {"_raw": raw, "_error": "non-json response from schema-constrained call"}


@router.post("/chat")
async def chat(
    request: Request,
    body: ChatRequest,
    caller: str = Depends(hitl_gate("ollama_chat")),
) -> dict[str, str]:
    """Plain-text generation. Returns `{"response": "<text>"}`."""
    del caller
    _proxy()  # an unset proxy is named before an unset default model
    text = await _complete(_calling_key(request), model=_model(body.model), prompt=body.prompt, system=body.system, schema=None)
    return {"response": text}


@router.get("/models")
async def models(request: Request, caller: str = Depends(require_api_token)) -> dict[str, Any]:
    """The chat models the inference proxy offers hive-tools' key.

    Read-only metadata; no HITL gate. Wraps the proxy's
    `GET /v1/models?purpose=chat`.

    Returns:
        {
            "proxy_url": "http://...",
            "default_model": "gpt-oss:20b-32k" | null,
            "models": [{"name": "...", "label": "...", "context_window": 32768}, ...]
        }
    """
    del caller
    root = _proxy()
    payload = await _send("GET", "/v1/models?purpose=chat", _calling_key(request))
    listed = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(listed, list):
        raise HTTPException(status_code=502, detail="inference proxy answered no model list")
    flat = [
        {
            "name": m.get("id", ""),
            "label": m.get("label"),
            "context_window": m.get("context_window"),
        }
        for m in listed
        if isinstance(m, dict)
    ]
    return {
        "proxy_url": root,
        "default_model": os.environ.get(DEFAULT_MODEL_VAR, "").strip() or None,
        "models": flat,
    }
