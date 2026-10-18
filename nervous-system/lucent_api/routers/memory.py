"""Memory (vector store) endpoints for the lucent-api nervous-system service.

Which mind a request speaks for is read off the credential it presented, never
off the request. `mind_id` used to arrive in the body — provenance on a write,
an opt-in filter on a read — behind one shared token every mind and every hook
on every host already held, so any holder could file a memory under any mind's
name and read every mind's memories. See `lucent_api.holder_access`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from hive_logging import log_event

from lucent_api.holder_access import Caller, require_holder

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
    #: Only a service acting on a mind's behalf, or an admin, may name this —
    #: and a mind may name the literal `shared` to write a hive-wide rule.
    #: Anything else is refused rather than quietly rescoped. It defaulted to
    #: "ada" for most of this store's life, which is how an unattributed
    #: write ended up with somebody's name on it.
    mind_id: str | None = None
    tier: str = "contextual"
    tags: str = ""
    source: str = "user"
    as_of: str | None = None
    expires_at: str | None = None
    recurring: bool | None = None
    codebase_ref: str | None = None


# The old query string was capped by uvicorn's request-line limit: a prompt
# past ~68 KB was refused at the HTTP layer, instantly, before the embedder
# was touched. A body has no such ceiling, and `_embed` is a synchronous call
# to Ollama, which serialises. A 200 KB prompt holds the embedder for ~22
# seconds, and every caller's timeout is 2 or 3 — so one oversized paste
# starves contextual retrieval for every mind on the hive, silently, because
# each hook swallows its timeout and injects nothing.
#
# So the ceiling is kept and made explicit. Well above any real prompt
# (the largest carry-forward this hive composes is capped at 120 KB and does
# not come through here), and a refusal rather than a slow success.
MAX_QUERY_BYTES = 64 * 1024


class RetrieveBody(BaseModel):
    """Body for ``POST /memory/retrieve``.

    The query is a prompt — a mind's whole turn, sometimes its soul and
    recent memory with it. It travels in the body because a URL carrying
    that lands verbatim in Zeek's http.log, in uvicorn's access log and in
    Loki, where the sentinel then reads a mind's own context back as
    network events.
    """

    query: str = Field(min_length=1)
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
    caller: Caller = Depends(require_holder),
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
            mind_ids=caller.minds_for_read(mind_id),
            tier=tier,
            data_class=data_class,
        )
    )


@router.get("/recent-decayed")
def memory_recent_decayed(
    caller: Caller = Depends(require_holder),
    mind_id: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
) -> Any:
    """Top-N contextual entries scored by recency decay (REQ-024).

    Optional ``mind_id`` filter — opt-in per-mind read, same shape as
    ``/memory/list`` and ``/memory/retrieve``.
    """
    from lucent_api.lucent_memory import query_decayed

    return _decode(
        query_decayed(limit=limit, mind_ids=caller.minds_for_read(mind_id))
    )


@router.post("/retrieve")
def memory_retrieve(
    body: RetrieveBody, caller: Caller = Depends(require_holder)
) -> Any:
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
    # Bytes, not characters: what the embedder and the wire both count, and a
    # prompt quoting a TUI transcript carries multi-byte box-drawing.
    size = len(body.query.encode("utf-8"))
    if size > MAX_QUERY_BYTES:
        raise HTTPException(
            413,
            f"query is {size} bytes; the limit is {MAX_QUERY_BYTES}. "
            "Refused here rather than held in the embedder, which is shared "
            "and serialises: a slow answer starves every other mind's turn.",
        )

    if body.mode == "hybrid":
        from lucent_api.lucent_memory import memory_retrieve_hybrid as _hybrid
        return _decode(
            _hybrid(
                query=body.query,
                k=body.k,
                mind_ids=caller.minds_for_read(body.mind_id),
                min_score=body.min_score,
                debug=body.debug,
            )
        )

    from lucent_api.lucent_memory import memory_retrieve as _memory_retrieve

    return _decode(
        _memory_retrieve(
            query=body.query,
            k=body.k,
            mind_ids=caller.minds_for_read(body.mind_id),
            tag_filter=body.tag_filter,
            data_class=body.data_class,
            min_score=body.min_score,
        )
    )


def _refuse_unless_owned(caller: Caller, memory_id: str) -> None:
    """Refuse a row the caller may not see.

    `/list` and `/retrieve` are filtered by provenance, but these two routes
    name a row directly, so without this a mind could edit or delete any
    memory on the hive by id — and ids are sequential integers.

    Absent rows answer the same 404 as somebody else's, so the ids of other
    minds' memories cannot be found by walking the range.
    """
    allowed = caller.minds_for_read(None)
    if allowed is None:
        return
    from lucent_api import holder_access

    row = holder_access.connection().execute(
        "SELECT mind_id FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    if row is None or str(row["mind_id"] or "") not in allowed:
        raise HTTPException(404, "Memory not found")


# ---- Write endpoints ----


@router.post("/store")
def memory_store(
    body: StoreBody, caller: Caller = Depends(require_holder)
) -> Any:
    """Store a memory with semantic embedding, filed under the mind whose
    credential was presented."""
    from lucent_api.lucent_memory import memory_store as _memory_store

    mind_id = caller.mind_for_write(body.mind_id)
    result = _decode(
        _memory_store(
            content=body.content,
            data_class=body.data_class,
            tier=body.tier,
            tags=body.tags,
            source=body.source,
            mind_id=mind_id,
            as_of=body.as_of,
            expires_at=body.expires_at,
            recurring=body.recurring,
            codebase_ref=body.codebase_ref,
        )
    )
    log_event(
        log, "memory.stored", memory_id=result.get("id") if isinstance(result, dict) else None,
        mind_id=mind_id, data_class=body.data_class, tier=body.tier,
        source=body.source, content_chars=len(body.content),
    )
    return result


@router.put("/{memory_id}")
def memory_update(
    memory_id: str, body: UpdateBody, caller: Caller = Depends(require_holder)
) -> Any:
    """Update an existing memory's content, data_class, or tags.

    Reassigning a memory to a different mind is an admin's or a service's
    move. A mind rewriting a row's provenance to its own would be a way to
    take one, and rewriting it to somebody else's a way to plant one.
    """
    from lucent_api.lucent_memory import memory_update as _memory_update

    _refuse_unless_owned(caller, memory_id)
    mind_id = caller.mind_for_write(body.mind_id) if body.mind_id else ""
    result = _decode(
        _memory_update(
            memory_id=memory_id,
            content=body.content,
            data_class=body.data_class,
            tags=body.tags,
            mind_id=mind_id,
        )
    )
    log_event(log, "memory.updated", memory_id=memory_id, mind_id=mind_id or None,
              data_class=body.data_class or None, content_chars=len(body.content))
    return result


@router.delete("/{memory_id}")
def memory_delete(
    memory_id: str, caller: Caller = Depends(require_holder)
) -> Any:
    """Delete a memory by ID, if it is the caller's to delete."""
    from lucent_api.lucent_memory import memory_delete as _memory_delete

    _refuse_unless_owned(caller, memory_id)
    result = _decode(_memory_delete(memory_id=memory_id))
    log_event(log, "memory.deleted", memory_id=memory_id)
    return result
