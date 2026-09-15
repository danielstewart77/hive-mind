"""Memory (vector store) endpoints for the lucent-api nervous-system service.

Wraps tools.stateful.lucent_memory public functions in FastAPI routes.
No auth and no HITL — internal Docker-network only.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from hive_logging import log_event

log = logging.getLogger(__name__)

router = APIRouter(prefix="/memory", tags=["memory"])


def _decode(payload: str) -> Any:
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return {"error": "invalid_json_from_underlying_function", "raw": payload}


# ---- Request schemas ----


class StoreBody(BaseModel):
    content: str
    data_class: str
    mind_id: str = "ada"
    tier: str = "contextual"
    tags: str = ""
    source: str = "user"
    as_of: str | None = None
    expires_at: str | None = None
    recurring: bool | None = None
    codebase_ref: str | None = None


class RetrieveBody(BaseModel):
    """Body for ``POST /memory/retrieve``.

    The query is a prompt — a mind's whole turn, sometimes its soul and
    recent memory with it. It travels in the body because a URL carrying
    that lands verbatim in Zeek's http.log, in uvicorn's access log and in
    Loki, where the sentinel then reads a mind's own context back as
    network events.
    """

    query: str
    mind_id: str | None = None
    k: int = Field(10, ge=1, le=50)
    tag_filter: str | None = None
    data_class: str | None = None
    min_score: float | None = Field(None, ge=0.0, le=1.0)
    mode: str = "vector"
    debug: bool = True


class UpdateBody(BaseModel):
    content: str = ""
    data_class: str = ""
    tags: str = ""
    mind_id: str = ""


# ---- Read endpoints ----


@router.get("/list")
def memory_list(
    mind_id: str | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    tier: str | None = Query(None),
    data_class: str | None = Query(None),
) -> Any:
    """List memories sequentially by creation time.

    Optional ``tier``, ``mind_id``, and ``data_class`` filters. ``mind_id``
    is opt-in — omit for cross-mind reads (default REQ-006 behavior),
    pass exactly when you want a provenance filter (e.g. the
    bootstrap-loader's per-mind + ``shared`` standing-rules union).
    ``data_class`` is required for scoped cleanup sweeps; it was silently
    ignored prior to 2026-05-25, which caused a full-table wipe via a
    delete loop that trusted it as a filter.
    """
    from lucent_api.lucent_memory import memory_list as _memory_list

    return _decode(
        _memory_list(
            offset=offset,
            limit=limit,
            mind_id=mind_id,
            tier=tier,
            data_class=data_class,
        )
    )


@router.get("/recent-decayed")
def memory_recent_decayed(
    mind_id: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
) -> Any:
    """Top-N contextual entries scored by recency decay (REQ-024).

    Optional ``mind_id`` filter — opt-in per-mind read, same shape as
    ``/memory/list`` and ``/memory/retrieve``.
    """
    from lucent_api.lucent_memory import query_decayed

    return _decode(query_decayed(limit=limit, mind_id=mind_id))


@router.post("/retrieve")
def memory_retrieve(body: RetrieveBody) -> Any:
    """Semantic search — return top-k memories most relevant to the query.

    Optional filters:
      mind_id    — provenance filter, opt-in (omit for cross-mind).
      data_class — keep only entries matching the given class.
      min_score  — drop entries below this cosine similarity (0.0–1.0).
      mode       — ``vector`` (default, legacy cosine-only) or ``hybrid``
                   (cosine + recency multiplier + BM25 via FTS5, returned
                   in fixed buckets).
      debug      — when ``mode=hybrid``, include per-row debug block with
                   bucket label and component scores.
    """
    if body.mode == "hybrid":
        from lucent_api.lucent_memory import memory_retrieve_hybrid as _hybrid
        return _decode(
            _hybrid(
                query=body.query,
                k=body.k,
                mind_id=body.mind_id,
                min_score=body.min_score,
                debug=body.debug,
            )
        )

    from lucent_api.lucent_memory import memory_retrieve as _memory_retrieve

    return _decode(
        _memory_retrieve(
            query=body.query,
            k=body.k,
            mind_id=body.mind_id,
            tag_filter=body.tag_filter,
            data_class=body.data_class,
            min_score=body.min_score,
        )
    )


# ---- Write endpoints ----


@router.post("/store")
def memory_store(body: StoreBody) -> Any:
    """Store a memory with semantic embedding."""
    from lucent_api.lucent_memory import memory_store as _memory_store

    result = _decode(
        _memory_store(
            content=body.content,
            data_class=body.data_class,
            tier=body.tier,
            tags=body.tags,
            source=body.source,
            mind_id=body.mind_id,
            as_of=body.as_of,
            expires_at=body.expires_at,
            recurring=body.recurring,
            codebase_ref=body.codebase_ref,
        )
    )
    log_event(
        log, "memory.stored", memory_id=result.get("id") if isinstance(result, dict) else None,
        mind_id=body.mind_id, data_class=body.data_class, tier=body.tier,
        source=body.source, content_chars=len(body.content),
    )
    return result


@router.put("/{memory_id}")
def memory_update(memory_id: str, body: UpdateBody) -> Any:
    """Update an existing memory's content, data_class, or tags."""
    from lucent_api.lucent_memory import memory_update as _memory_update

    result = _decode(
        _memory_update(
            memory_id=memory_id,
            content=body.content,
            data_class=body.data_class,
            tags=body.tags,
            mind_id=body.mind_id,
        )
    )
    log_event(log, "memory.updated", memory_id=memory_id, mind_id=body.mind_id or None,
              data_class=body.data_class or None, content_chars=len(body.content))
    return result


@router.delete("/{memory_id}")
def memory_delete(memory_id: str) -> Any:
    """Delete a memory by ID."""
    from lucent_api.lucent_memory import memory_delete as _memory_delete

    result = _decode(_memory_delete(memory_id=memory_id))
    log_event(log, "memory.deleted", memory_id=memory_id)
    return result
