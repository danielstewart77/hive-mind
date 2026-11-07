"""Lucent's memory tools, over the hive's Postgres through `hive_data`.

What changed and why it matters. This module used to own its storage: a
SQLite connection per thread, embeddings as `float32` blobs, and a cosine
scan that pulled *every* row's 4096 floats into numpy to rank them in
Python. That scan is the one shape that cannot cross to Postgres. The
access layer's pgvector codec is text-format, so the same `SELECT ...
embedding ... FROM memories` with no `LIMIT` would send hundreds of
megabytes as text literals and parse them back one `float()` at a time,
inside a 30-second statement deadline, on the event loop. So the reads here
call `hive_data.memory`'s named operations — `similar`, `keyword`, `recall`
— and the ranking runs in the database.

Three behaviours deliberately changed with the move, all of them in the
access layer and none of them to be "fixed" back here:

* A **blank** provenance matches nothing. lucent's own filter tested
  `if mind_id:`, so the empty string an unset `MIND_ID` produces made the
  clause vanish and the read returned every mind's memories — the kids'
  boxes alongside the operator's.
* A long query ranks on its first `MAX_QUERY_TERMS` words.
* Keyword scores are `ts_rank` scaled, not FTS5 bm25, so the figures and
  which rows land in the keyword bucket both move. `memories_fts` did not
  exist in this install's SQLite file at all, so lucent's keyword half has
  been silently dead inside a `try/except`; after the move it works.

Nothing here returns `{"error": ...}` for a store that did not answer any
more. `hive_data.Unreachable`, `TimedOut`, `NoConnectionAvailable`,
`TransactionAborted` and `Conflict` are routine against a database over a
socket, and the callers are detached `bash + jq + curl` hooks that check
nothing — so a two-hundred with an error in the body is a write that never
happened reported as a turn that was remembered. They propagate, and
`lucent_api.routers.memory` turns each into a status code that names the
rule that refused it.

Model: qwen3-embedding:8b via Ollama (4096-dim).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from collections.abc import Sequence
from typing import Any, Optional

import requests

import hive_data
from hive_data import memory as store
from hive_data.schema import qualified

from lucent_api.memory_schema import (
    build_metadata,
    validate_source,
    validate_tier,
)
from lucent_api.secrets import get_credential

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8420")
HITL_TTL = 180

OLLAMA_BASE_URL = get_credential("OLLAMA_BASE_URL") or "http://127.0.0.1:11434"
EMBEDDING_MODEL = "qwen3-embedding:8b"
EMBEDDING_DIM = 4096

DEDUP_THRESHOLD = 0.92  # cosine similarity at or above which a write is deduped
DECAY_HALF_LIFE_DAYS = 14  # half-life for the recency decay score

#: The memories table as a statement has to spell it. Named here because
#: three reads below have no operation of their own in the access layer yet —
#: a paged listing and an update are a later story — and none of them names
#: `embedding`.
_MEMORIES = qualified("memories")


def _embed(text: str) -> list[float]:
    """The embedding for a text, from Ollama. Synchronous on purpose.

    Kept as the one synchronous call in the module because it is the point
    where the process stops being ours — and it is therefore the one thing a
    test stubs. Nothing on a route calls it directly; `_embedding_for` does.
    """
    resp = requests.post(
        f"{OLLAMA_BASE_URL}/api/embed",
        json={"model": EMBEDDING_MODEL, "input": text},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["embeddings"][0]


async def _embedding_for(text: str) -> list[float]:
    """`_embed` off the event loop.

    Every route in this service is `async def` now, which means uvicorn no
    longer hands it a worker thread. A 30-second `requests.post` awaited
    inline would block the loop itself — every other route, the connection
    pool's own protocol handling, and `/health` with it — so the one
    blocking call left goes to a thread.
    """
    return await asyncio.to_thread(_embed, text)


async def _hitl_gate(content: str) -> bool:
    """Request HITL approval showing the exact content to be stored.

    In a thread for the same reason as `_embedding_for`, and more so: this
    one waits up to `HITL_TTL + 5` seconds for a human.
    """

    def _ask() -> bool:
        resp = requests.post(
            f"{GATEWAY_URL}/hitl/request",
            json={"action": "memory_store", "summary": content,
                  "ttl": HITL_TTL},
            timeout=HITL_TTL + 5,
        )
        resp.raise_for_status()
        return bool(resp.json().get("approved", False))

    try:
        return await asyncio.to_thread(_ask)
    except Exception:
        logger.exception("HITL gate failed -- denying memory write by default")
        return False


def _mind_filter(
    mind_id: str | None,
    mind_ids: Sequence[str] | None,
    start: int = 1,
) -> tuple[str, list[Any]]:
    """The provenance clause for a read, as SQL and arguments.

    `hive_data.memory._provenance`'s rule, spelled in numbered placeholders
    for the reads below that the access layer has no named operation for. It
    is the access layer's rule and not lucent's old one, which is the point:
    `if mind_id:` made the clause disappear on the empty string an unset
    `MIND_ID` produces, and a scoping check that becomes a scoping bypass on
    exactly that value is how one mind reads another's memories.

    Blank — an empty list, or a name that is only whitespace — matches
    nothing. `None` means no restriction at all, which is what the console's
    own pages and the nightly pruner want.
    """
    if mind_ids is not None:
        if isinstance(mind_ids, (str, bytes)):
            # A `str` is a `Sequence[str]`, so no type checker catches this,
            # and iterating one gives its characters: "skippy" becomes six
            # one-letter minds and matches nothing, which reads as "this
            # mind has no memories about that".
            raise hive_data.ValueRefused(
                f"mind_ids was given the single name {mind_ids!r} rather "
                "than a sequence of names. Pass [name], or use mind_id.")
        names = [str(m).strip() for m in mind_ids if str(m or "").strip()]
        if not names:
            return "1 = 0", []
        return f'"mind_id" = ANY(${start}::text[])', [names]
    if mind_id is None:
        return "", []
    named = str(mind_id).strip()
    if not named:
        return "1 = 0", []
    return f'"mind_id" = ${start}', [named]


async def memory_store_direct(
    *,
    content: str,
    data_class: str,
    tier: str = "contextual",
    tags: str = "",
    source: str = "user",
    mind_id: str,
    as_of: str | None = None,
    expires_at: str | None = None,
    recurring: bool | None = None,
    codebase_ref: str | None = None,
) -> str:
    """Write to vector memory without HITL.

    Tier defaults to ``contextual``. Pass ``tier="standing"`` only from
    /always-remember. Skips the insert when an entry of the same data_class
    already exists with cosine similarity >= DEDUP_THRESHOLD.
    """
    try:
        validate_source(source)
    except ValueError as e:
        return json.dumps({"stored": False, "error": str(e)})

    try:
        validate_tier(tier)
    except ValueError as e:
        return json.dumps({"stored": False, "error": str(e)})

    # REQ-028: standing tier may only be written from /always-remember.
    if tier == "standing" and source != "always-remember":
        return json.dumps({
            "stored": False,
            "error": (
                "tier=standing requires source='always-remember'; "
                f"got source={source!r}. Use the /always-remember skill instead."
            ),
        })

    try:
        meta = build_metadata(
            data_class=data_class,
            source=source,
            as_of=as_of,
            expires_at=expires_at,
            recurring=recurring,
            content=content,
        )
    except ValueError as e:
        return json.dumps({"stored": False, "error": str(e), "prompt": str(e)})

    embedding = await _embedding_for(content)

    # REQ-049: save-time dedup against the same data_class, and only the
    # same data_class — which is why `data_class` is an argument to the
    # access layer's own `similar` rather than a filter applied to its
    # answer. The nearest row in the store overall can score over
    # `DEDUP_THRESHOLD` while belonging to a different class entirely, and
    # that answers `{"deduped": true}` and never writes the turn's memory
    # while the hook records a success.
    nearest = await store.similar(embedding, limit=1, data_class=data_class)
    if nearest and float(nearest[0]["score"]) >= DEDUP_THRESHOLD:
        return json.dumps({
            "deduped": True,
            "existing_id": int(nearest[0]["id"]),
            "score": round(float(nearest[0]["score"]), 4),
        })

    written = await store.store(
        mind_id=mind_id,
        content=content,
        embedding=embedding,
        tags=tags,
        source=meta.get("source", source),
        data_class=meta.get("data_class"),
        tier=tier,
        as_of=meta.get("as_of"),
        expires_at=meta.get("expires_at"),
        superseded=1 if meta.get("superseded") else 0,
        recurring=(
            1 if meta.get("recurring")
            else (0 if meta.get("recurring") is False else None)
        ),
        codebase_ref=codebase_ref,
        created_at=int(time.time()),
    )
    row = (written or {}).get("row") or {}

    return json.dumps({
        "stored": True,
        "id": row.get("id"),
        "mind_id": mind_id,
        "data_class": meta.get("data_class"),
        "tier": tier,
    })


async def memory_store(
    *,
    content: str,
    data_class: str,
    tier: str = "contextual",
    tags: str = "",
    source: str = "user",
    mind_id: str,
    as_of: str | None = None,
    expires_at: str | None = None,
    recurring: bool | None = None,
    codebase_ref: str | None = None,
) -> str:
    """Store a memory as a semantic embedding.

    Args:
        content: The text to remember.
        data_class: Memory data class (e.g. "person", "preference"). Required.
        tier: Retrieval tier — ``contextual`` (default) or ``standing``
            (always-on). Passing ``standing`` from non-always-remember
            sources is allowed at this layer but discouraged.
        tags: Comma-separated tags for categorisation.
        source: Origin of the memory -- "user", "tool", "session", "self".
        mind_id: Which mind this memory belongs to. Required, and no longer
            defaulted: it used to default to "ada", which is how an
            unattributed write ended up with somebody's name on it. Over HTTP
            it is read off the presented credential and the body cannot set
            it; in-process callers have to say.
        as_of: ISO datetime for when the fact was established.
        expires_at: ISO datetime for when a timed-event expires.
        recurring: Explicit recurring flag for timed-events.
        codebase_ref: Optional file path or symbol reference.

    Returns:
        JSON with the stored memory ID and confirmation, or {"deduped": True}
        when an entry with cosine similarity >= 0.92 already exists in the
        same data_class.
    """
    return await memory_store_direct(
        content=content,
        tier=tier,
        tags=tags,
        source=source,
        mind_id=mind_id,
        data_class=data_class,
        as_of=as_of,
        expires_at=expires_at,
        recurring=recurring,
        codebase_ref=codebase_ref,
    )


#: What `/memory/list` hands back per row. Narrower than the table on
#: purpose: the anchor columns are not in it, which is why the pruner reads
#: through `hive_data.graph.memories_for_class` rather than through here.
_LIST_COLUMNS = (
    "id", "content", "tags", "source", "data_class", "tier", "mind_id",
    "created_at",
)


async def memory_list(
    offset: int = 0,
    limit: int = 25,
    mind_id: str | None = None,
    mind_ids: Sequence[str] | None = None,
    tier: str | None = None,
    data_class: str | None = None,
) -> str:
    """List memories sequentially by creation time for review and cleanup.

    Args:
        offset: Number of entries to skip (for pagination).
        limit: Number of entries to return (default 25, max 100).
        mind_id: Optional filter. When set, returns only entries whose
            provenance ``mind_id`` matches exactly. When omitted, returns
            entries from every mind (cross-mind read; REQ-006's original
            "provenance only, never a filter" still applies as the default).
            The standing-rules bootstrap path uses this to fetch a mind's
            own rules plus the "shared" sentinel with two calls and union
            client-side.
        tier: Optional filter — when set, only entries matching this tier
            are returned (and counted in total). Useful for the bootstrap
            loader's standing-tier subset.
        data_class: Optional filter — when set, only entries whose
            ``data_class`` matches exactly are returned. Required for
            scoped cleanup sweeps (e.g. deleting every ``ephemeral`` row);
            an earlier silent-ignore behaviour caused a full-table wipe
            when callers passed this thinking it was honoured.

    Returns:
        JSON with entries, offset, limit, and total count.
    """
    limit = min(limit, 100)

    conditions: list[str] = []
    args: list[Any] = []
    if tier:
        args.append(tier)
        conditions.append(f'"tier" = ${len(args)}')
    if data_class:
        args.append(data_class)
        conditions.append(f'"data_class" = ${len(args)}')
    clause, clause_args = _mind_filter(mind_id, mind_ids, len(args) + 1)
    if clause:
        conditions.append(clause)
        args.extend(clause_args)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    columns = ", ".join(f'"{c}"' for c in _LIST_COLUMNS)
    count_sql = f'SELECT count(*) AS "total" FROM {_MEMORIES} {where}'
    # `created_at` then `id`: epoch seconds, so a hook writing twice inside
    # one second gives two rows the same sort key, and an order that is not
    # total silently repeats and skips rows as the caller pages through.
    page_sql = (f'SELECT {columns} FROM {_MEMORIES} {where} '
                f'ORDER BY "created_at" ASC, "id" ASC '
                f'LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}')

    async def _read(connection: Any) -> tuple[int, list[Any]]:
        total = await connection.fetchval(count_sql, *args)
        rows = await connection.fetch(page_sql, *args, limit, offset)
        return int(total or 0), rows

    total, rows = await hive_data.run(_read)
    entries = [{c: row[c] for c in _LIST_COLUMNS} for row in rows]
    return json.dumps({"entries": entries, "offset": offset,
                       "limit": limit, "total": total})


async def memory_delete(memory_id: str) -> str:
    """Delete a memory by its ID.

    Args:
        memory_id: The ID of the memory to delete.

    Returns:
        JSON confirming deletion or error.
    """
    try:
        target = int(memory_id)
    except (TypeError, ValueError):
        return json.dumps({"deleted": False, "reason": "not found",
                           "id": memory_id})

    row = await store.get(target)
    if row is None:
        return json.dumps({"deleted": False, "reason": "not found",
                           "id": memory_id})
    result = await store.delete(target)
    if not result.get("rows"):
        # Read and then gone: somebody else deleted it between the two
        # statements. Reported as not found rather than as a deletion,
        # because the caller's next move is a retry either way.
        return json.dumps({"deleted": False, "reason": "not found",
                           "id": memory_id})
    return json.dumps({"deleted": True, "id": memory_id,
                       "content": row["content"]})


async def memory_update(
    memory_id: str,
    content: str = "",
    data_class: str = "",
    tags: str = "",
    mind_id: str = "",
) -> str:
    """Update an existing memory.

    Args:
        memory_id: The ID of the memory to update.
        content: New content to store. Re-embeds automatically.
        data_class: New data class to assign.
        tags: Comma-separated tags to replace existing tags.
        mind_id: Reassign the memory to a different mind. Pass the canonical
            UUID or the literal "shared" sentinel. Empty leaves the field
            untouched.

    Returns:
        JSON with updated memory details or error.
    """
    from lucent_api.memory_schema import DATA_CLASS_REGISTRY, validate_data_class

    try:
        target = int(memory_id)
    except (TypeError, ValueError):
        return json.dumps({"updated": False, "error": "not found",
                           "id": memory_id})

    assignments: list[str] = []
    args: list[Any] = []

    def _set(column: str, value: Any) -> None:
        args.append(value)
        assignments.append(f'"{column}" = ${len(args)}')

    if content:
        _set("content", content)
        _set("embedding", await _embedding_for(content))

    if data_class:
        try:
            validate_data_class(data_class)
        except ValueError as e:
            return json.dumps({"updated": False, "error": str(e)})
        _set("data_class", data_class)
        _set("tier", DATA_CLASS_REGISTRY[data_class].tier)

    if tags:
        _set("tags", tags)

    if mind_id:
        _set("mind_id", mind_id)

    if not assignments:
        return json.dumps({"updated": False,
                           "error": "no fields provided to update"})

    args.append(target)
    # One statement rather than an update followed by a read: the row it
    # returns is the row it wrote, so a concurrent write cannot make the
    # answer describe a different version than the one that landed. Nothing
    # names `content_search` — it is GENERATED ALWAYS and recomputed from
    # the new content by this same statement, so a memory stays findable by
    # word with no window in which it is updated and unsearchable.
    sql = (f'UPDATE {_MEMORIES} SET {", ".join(assignments)} '
           f'WHERE "id" = ${len(args)} '
           f'RETURNING "data_class", "mind_id", '
           f'substr("content", 1, 80) AS "preview"')
    row = await hive_data.run(lambda c: c.fetchrow(sql, *args))

    if row is None:
        return json.dumps({"updated": False, "error": "not found",
                           "id": memory_id})
    return json.dumps({
        "updated": True,
        "id": target,
        "data_class": row["data_class"],
        "mind_id": row["mind_id"],
        "preview": row["preview"],
    })


#: The keys a `/memory/retrieve` row carries, in lucent's own order. `id` is
#: deliberately not among them — this route has never returned one, and a
#: caller reading the answer by key sees exactly these.
_RETRIEVE_KEYS = (
    "content", "tags", "source", "mind_id", "created_at", "data_class",
    "tier", "as_of", "expires_at", "superseded", "codebase_ref",
)


async def memory_retrieve(
    query: str,
    k: int = 10,
    mind_id: str | None = None,
    mind_ids: Sequence[str] | None = None,
    tag_filter: Optional[str] = None,
    data_class: Optional[str] = None,
    min_score: Optional[float] = None,
) -> str:
    """Retrieve the most semantically relevant memories for a query.

    Args:
        query: Natural language query to search for related memories.
        k: Number of results to return (default 10, max 50).
        mind_id: Optional filter. When set, returns only entries whose
            provenance ``mind_id`` matches exactly. When omitted, returns
            entries from every mind (cross-mind read, REQ-006's original
            default). The contextual-retrieval hook uses this to fetch a
            mind's own behaviour-rule feedback plus the "shared" sentinel
            via two calls and union — same pattern as ``memory_list``.
        tag_filter: Optional tag to filter results. Matched
            case-insensitively, as it was under SQLite.
        data_class: Optional data class filter — keep only entries with the
            given data_class.
        min_score: Optional cosine-similarity threshold — keep only entries
            with score >= min_score.

    Returns:
        JSON array of memories sorted by relevance (highest first).
    """
    k = min(k, 50)

    embedding = await _embedding_for(query)
    # The class and tag filters are arguments to the ranking rather than a
    # pass over its results: filtering afterwards is how a read with a
    # `data_class` answers nothing while the store holds twenty matching
    # rows that merely sat below the top k. The tag is matched
    # case-insensitively in there, which `LIKE` was under SQLite and is not
    # here. `min_score` is a threshold on the sort key itself, so applying
    # it to the ranked page is the same answer as applying it to the whole
    # store.
    rows = await store.similar(
        embedding, limit=k, mind_id=mind_id, mind_ids=mind_ids,
        data_class=data_class, tag=tag_filter or None)

    memories = []
    for row in rows:
        score = round(float(row["score"]), 4)
        if min_score is not None and score < min_score:
            continue
        out = {key: row.get(key) for key in _RETRIEVE_KEYS}
        flag = out.get("superseded")
        out["superseded"] = None if flag is None else bool(flag)
        out["score"] = score
        memories.append(out)

    return json.dumps({"memories": memories, "count": len(memories)})


async def query_decayed(
    limit: int = 20,
    mind_id: str | None = None,
    mind_ids: Sequence[str] | None = None,
) -> str:
    """Return top-N contextual entries scored by recency decay.

    Score: ``exp(-(now - created_at) / (DECAY_HALF_LIFE_DAYS * 86400))``.
    Standing-tier rows are excluded — they are loaded unconditionally at
    bootstrap so they should not also occupy the recent-memory budget.

    Args:
        limit: max rows to return (default 20).
        mind_id: optional provenance filter — opt-in per-mind read (same
            shape as memory_list / memory_retrieve). When set, returns
            only rows whose mind_id matches exactly. When omitted, returns
            rows from every mind (cross-mind default).

    Returns:
        JSON-encoded list of dicts: id, content, data_class, score,
        created_at, mind_id.
    """
    conditions = ["\"tier\" = 'contextual'"]
    args: list[Any] = []
    clause, clause_args = _mind_filter(mind_id, mind_ids, len(args) + 1)
    if clause:
        conditions.append(clause)
        args.extend(clause_args)
    where = "WHERE " + " AND ".join(conditions)

    # The decay is strictly decreasing in age, so the top N by score are the
    # N newest and the ordering can be the database's. lucent read every
    # contextual row of every mind to find twenty of them.
    sql = (f'SELECT "id", "content", "data_class", "mind_id", "created_at" '
           f'FROM {_MEMORIES} {where} '
           f'ORDER BY "created_at" DESC NULLS LAST, "id" DESC '
           f'LIMIT ${len(args) + 1}')
    rows = await hive_data.run(lambda c: c.fetch(sql, *args, int(limit)))

    now = time.time()
    half_life_seconds = DECAY_HALF_LIFE_DAYS * 86400
    scored = [
        {
            "id": row["id"],
            "content": row["content"],
            "data_class": row["data_class"],
            "mind_id": row["mind_id"],
            "created_at": row["created_at"],
            "score": round(
                math.exp(-(now - float(row["created_at"] or 0))
                         / half_life_seconds), 4),
        }
        for row in rows
    ]
    return json.dumps(scored)


HYBRID_RECENCY_HALF_LIFE_DAYS = store.RECENCY_HALF_LIFE_DAYS
HYBRID_RECENCY_FLOOR = store.RECENCY_FLOOR
HYBRID_VECTOR_POOL = store.VECTOR_POOL
HYBRID_BM25_POOL = store.KEYWORD_POOL
HYBRID_DEFAULT_K = store.DEFAULT_K


async def memory_retrieve_hybrid(
    query: str,
    k: int = HYBRID_DEFAULT_K,
    mind_id: str | None = None,
    mind_ids: Sequence[str] | None = None,
    min_score: float | None = None,
    debug: bool = True,
) -> str:
    """Hybrid retrieval: vector + recency-weighted + keyword, in one answer.

    `hive_data.memory.recall` is the whole of it. The blend is lucent's own,
    carried across with its constants and its bucket arithmetic unchanged —
    three slots to the best ``cosine * recency``, one to the best raw
    cosine, one to the best keyword hit, the remainder filled from a
    combined ranking — and the only thing that moved is where the
    arithmetic runs.

    Two differences that are the point of the move rather than regressions.
    The candidate pool is now the top `VECTOR_POOL` by cosine from the
    database, where lucent scored every row of every mind in Python. And the
    keyword half works: `memories_fts` does not exist in this install's
    SQLite file at all, so lucent's keyword lookup has been failing into a
    `logger.exception` and answering vector-only every turn since it was
    written.

    There is deliberately no `try/except` around the keyword half here.
    `hive_data` caps a query at `MAX_QUERY_TERMS` so the one failure lucent
    was guarding against — a pasted transcript parsed as an OR of sixty
    thousand terms — cannot arrive; and a handler here would swallow a
    rejected write reported on a read, degrading hybrid to vector-only under
    the same ``"mode": "hybrid"`` label, with worse answers and one log
    line.
    """
    embedding = await _embedding_for(query)
    answer = await store.recall(
        query=query,
        embedding=embedding,
        k=k,
        mind_id=mind_id,
        mind_ids=mind_ids,
        min_score=min_score,
        debug=debug,
    )
    return json.dumps(answer)


# All memory tool functions for registration
MEMORY_TOOLS = [
    memory_store,
    memory_store_direct,
    memory_list,
    memory_delete,
    memory_update,
    memory_retrieve,
    memory_retrieve_hybrid,
    query_decayed,
]
