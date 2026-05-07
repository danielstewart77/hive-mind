"""HTTP client for the shared hive_nervous_system lucent service.

The single Python entry point for memory + graph writes from hive_mind code.
Reads `LUCENT_URL` and `LUCENT_BEARER_TOKEN` from the environment and
attaches the bearer header to every request. All endpoints listed here are
thin pass-throughs to the shared container — see
specs/memory-system-implementation.md (Part I) for the full endpoint table.

Sync (uses `requests`) because callers (core/epilogue.py, server.py
`/graph/data`) are sync. If an async caller appears, add an `aiohttp`-based
sibling rather than wrapping these in a thread.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0


class LucentClientError(RuntimeError):
    """Raised on transport-level failure (401/5xx, network, timeout)."""


def _base_url() -> str:
    url = os.environ.get("LUCENT_URL", "").rstrip("/")
    if not url:
        raise LucentClientError("LUCENT_URL is not set")
    return url


def _headers() -> dict[str, str]:
    token = os.environ.get("LUCENT_BEARER_TOKEN", "")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _request(method: str, path: str, **kwargs: Any) -> dict:
    url = f"{_base_url()}{path}"
    timeout = kwargs.pop("timeout", DEFAULT_TIMEOUT)
    try:
        resp = requests.request(method, url, headers=_headers(), timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        raise LucentClientError(f"{method} {path}: network error: {exc}") from exc

    if resp.status_code == 401:
        raise LucentClientError(f"{method} {path}: 401 unauthorized (check LUCENT_BEARER_TOKEN)")
    if resp.status_code >= 500:
        raise LucentClientError(f"{method} {path}: {resp.status_code} {resp.text[:200]}")

    try:
        return resp.json()
    except ValueError as exc:
        raise LucentClientError(
            f"{method} {path}: non-JSON response (status {resp.status_code}): {resp.text[:200]}"
        ) from exc


def health_check() -> bool:
    try:
        resp = requests.get(f"{_base_url()}/health", timeout=DEFAULT_TIMEOUT)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def memory_store(
    *,
    content: str,
    data_class: str,
    agent_id: str,
    source: str,
    tier: str = "contextual",
    tags: str = "",
    expires_at: str | None = None,
    codebase_ref: str | None = None,
) -> dict:
    """Write a memory entry. Returns the parsed response dict.

    Possible shapes (handle all three):
      - {"id": <new_id>, ...}             — fresh insert
      - {"deduped": true, "existing_id": N, "score": 1.0}  — dedup hit
      - {"stored": false, "error": "..."}  — application-level rejection
    """
    payload: dict[str, Any] = {
        "content": content,
        "data_class": data_class,
        "agent_id": agent_id,
        "source": source,
        "tier": tier,
        "tags": tags,
    }
    if expires_at is not None:
        payload["expires_at"] = expires_at
    if codebase_ref is not None:
        payload["codebase_ref"] = codebase_ref
    return _request("POST", "/memory/store", json=payload)


def graph_upsert_direct(
    *,
    entity_type: str,
    name: str,
    agent_id: str,
    source: str,
    data_class: str = "",
    tier: str = "contextual",
    properties: str = "{}",
    relation: str = "",
    target_name: str = "",
    target_type: str = "",
) -> dict:
    """Write a node (and optional edge) directly. Returns parsed response.

    Skips the disambiguation/orphan guards (the `-direct` variant). The
    identity guard (type='Mind') still applies server-side.

    `properties` is a string-encoded JSON blob — full-replace semantics.
    Read existing properties first if you need to merge.
    """
    payload = {
        "entity_type": entity_type,
        "name": name,
        "agent_id": agent_id,
        "source": source,
        "data_class": data_class,
        "tier": tier,
        "properties": properties,
        "relation": relation,
        "target_name": target_name,
        "target_type": target_type,
    }
    return _request("POST", "/graph/upsert-direct", json=payload)


def graph_data(limit: int = 400) -> dict:
    """Visualization export — nodes + edges. Used by /graph/data proxy."""
    return _request("GET", "/graph/data", params={"limit": limit})
