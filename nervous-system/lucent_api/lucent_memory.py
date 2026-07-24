"""Lucent memory tools -- SQLite-backed drop-in replacement for memory.py.

Provides identical function signatures and JSON return shapes to
tools/stateful/memory.py, backed by the Lucent SQLite database
with numpy-based cosine similarity instead of Neo4j vector index.

Model: qwen3-embedding:8b via Ollama (4096-dim)
Backend: SQLite with numpy cosine similarity

Designed for direct FastMCP registration (no @tool() decorator).
"""

import json
import logging
import os
import time
from typing import Optional

import numpy as np
import requests
from lucent_api.memory_schema import build_metadata, validate_source, validate_tier
from lucent_api.secrets import get_credential

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8420")
HITL_TTL = 180

OLLAMA_BASE_URL = get_credential("OLLAMA_BASE_URL") or "http://203.0.113.10:11434"
EMBEDDING_MODEL = "qwen3-embedding:8b"
EMBEDDING_DIM = 4096

DEDUP_THRESHOLD = 0.92  # cosine similarity at or above which a write is deduped
DECAY_HALF_LIFE_DAYS = 14  # half-life for the recency decay score


def _hitl_gate(content: str) -> bool:
    """Request HITL approval showing the exact content to be stored."""
    try:
        resp = requests.post(
            f"{GATEWAY_URL}/hitl/request",
            json={"action": "memory_store", "summary": content, "ttl": HITL_TTL},
            timeout=HITL_TTL + 5,
        )
        resp.raise_for_status()
        return resp.json().get("approved", False)
    except Exception:
        logger.exception("HITL gate failed -- denying memory write by default")
        return False


def _embed(text: str) -> list[float]:
    resp = requests.post(
        f"{OLLAMA_BASE_URL}/api/embed",
        json={"model": EMBEDDING_MODEL, "input": text},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["embeddings"][0]


def _get_conn():
    """Lazy import to get the Lucent SQLite connection."""
    from lucent_api.lucent import _get_connection
    return _get_connection()


def _decode_embedding(raw) -> np.ndarray | None:
    """Decode a stored embedding to float32 ndarray.

    Tolerates legacy rows where the column was written as a JSON-encoded
    string (e.g. '[0.025, 0.013, ...]') instead of the canonical
    np.float32 .tobytes() blob. Returns None if the value is unusable.
    """
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray, memoryview)):
        return np.frombuffer(bytes(raw), dtype=np.float32)
    if isinstance(raw, str):
        try:
            return np.array(json.loads(raw), dtype=np.float32)
        except (ValueError, TypeError):
            return None
    return None


def _nearest_neighbour(
    embedding: np.ndarray,
    data_class: str,
) -> tuple[int, float] | None:
    """Return (id, score) of the highest-cosine-similarity memory in the same data_class.

    Used by save-time dedup. Returns None when no rows exist in the class.
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, embedding FROM memories WHERE data_class = ?",
        (data_class,),
    ).fetchall()
    if not rows:
        return None

    norm_q = float(np.linalg.norm(embedding))
    if norm_q == 0:
        return None

    best_id: int | None = None
    best_score = -1.0
    for row in rows:
        emb = _decode_embedding(row["embedding"])
        if emb is None:
            continue
        norm_e = float(np.linalg.norm(emb))
        if norm_e == 0:
            continue
        score = float(np.dot(embedding, emb) / (norm_q * norm_e))
        if score > best_score:
            best_score = score
            best_id = int(row["id"])

    if best_id is None:
        return None
    return best_id, best_score


def memory_store_direct(
    *,
    content: str,
    data_class: str,
    tier: str = "contextual",
    tags: str = "",
    source: str = "user",
    mind_id: str = "ada",
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

        embedding = _embed(content)
        embedding_arr = np.array(embedding, dtype=np.float32)

        # REQ-049: save-time dedup against same data_class.
        nn = _nearest_neighbour(embedding_arr, data_class)
        if nn is not None and nn[1] >= DEDUP_THRESHOLD:
            return json.dumps({
                "deduped": True,
                "existing_id": nn[0],
                "score": round(nn[1], 4),
            })

        embedding_blob = embedding_arr.tobytes()

        conn = _get_conn()
        cursor = conn.execute(
            """
            INSERT INTO memories (
                mind_id, content, embedding, tags, source,
                data_class, tier, as_of, expires_at,
                superseded, recurring, codebase_ref, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mind_id, content, embedding_blob, tags,
                meta.get("source", source),
                meta.get("data_class"), tier, meta.get("as_of"),
                meta.get("expires_at"),
                1 if meta.get("superseded") else 0,
                1 if meta.get("recurring") else (0 if meta.get("recurring") is False else None),
                codebase_ref,
                int(time.time()),
            ),
        )
        conn.commit()
        memory_id = cursor.lastrowid

        return json.dumps({
            "stored": True,
            "id": memory_id,
            "mind_id": mind_id,
            "data_class": meta.get("data_class"),
            "tier": tier,
        })
    except Exception as e:
        logger.exception("memory_store_direct failed")
        return json.dumps({"error": str(e)})


def memory_store(
    *,
    content: str,
    data_class: str,
    tier: str = "contextual",
    tags: str = "",
    source: str = "user",
    mind_id: str = "ada",
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
        mind_id: Which agent this memory belongs to (default "ada"). Stored
            as provenance only — reads do not filter by mind_id.
        as_of: ISO datetime for when the fact was established.
        expires_at: ISO datetime for when a timed-event expires.
        recurring: Explicit recurring flag for timed-events.
        codebase_ref: Optional file path or symbol reference.

    Returns:
        JSON with the stored memory ID and confirmation, or {"deduped": True}
        when an entry with cosine similarity >= 0.92 already exists in the
        same data_class.
    """
    try:
        return memory_store_direct(
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
    except Exception as e:
        logger.exception("memory_store failed")
        return json.dumps({"error": str(e)})


def memory_list(
    offset: int = 0,
    limit: int = 25,
    mind_id: str | None = None,
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

    where_parts: list[str] = []
    params: list = []
    if tier:
        where_parts.append("tier = ?")
        params.append(tier)
    if mind_id:
        where_parts.append("mind_id = ?")
        params.append(mind_id)
    if data_class:
        where_parts.append("data_class = ?")
        params.append(data_class)
    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    try:
        conn = _get_conn()
        total_row = conn.execute(
            f"SELECT COUNT(*) AS total FROM memories {where_clause}",
            params,
        ).fetchone()
        total = total_row["total"]

        rows = conn.execute(
            f"""
            SELECT id, content, tags, source, data_class, tier, mind_id, created_at
            FROM memories
            {where_clause}
            ORDER BY created_at ASC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()

        entries = [
            {
                "id": row["id"],
                "content": row["content"],
                "tags": row["tags"],
                "source": row["source"],
                "data_class": row["data_class"],
                "tier": row["tier"],
                "mind_id": row["mind_id"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

        return json.dumps({"entries": entries, "offset": offset, "limit": limit, "total": total})
    except Exception as e:
        logger.exception("memory_list failed")
        return json.dumps({"error": str(e)})


def memory_delete(memory_id: str) -> str:
    """Delete a memory by its ID.

    Args:
        memory_id: The ID of the memory to delete.

    Returns:
        JSON confirming deletion or error.
    """
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT id, content, mind_id FROM memories WHERE id = ?",
            (int(memory_id),),
        ).fetchone()

        if not row:
            return json.dumps({"deleted": False, "reason": "not found", "id": memory_id})

        content = row["content"]

        conn.execute("DELETE FROM memories WHERE id = ?", (int(memory_id),))
        conn.commit()

        return json.dumps({"deleted": True, "id": memory_id, "content": content})
    except Exception as e:
        logger.exception("memory_delete failed")
        return json.dumps({"error": str(e)})


def memory_update(
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
        set_parts = []
        params: list = []

        if content:
            embedding = _embed(content)
            embedding_blob = np.array(embedding, dtype=np.float32).tobytes()
            set_parts.append("content = ?")
            params.append(content)
            set_parts.append("embedding = ?")
            params.append(embedding_blob)

        if data_class:
            try:
                validate_data_class(data_class)
            except ValueError as e:
                return json.dumps({"updated": False, "error": str(e)})
            set_parts.append("data_class = ?")
            params.append(data_class)
            set_parts.append("tier = ?")
            params.append(DATA_CLASS_REGISTRY[data_class].tier)

        if tags:
            set_parts.append("tags = ?")
            params.append(tags)

        if mind_id:
            set_parts.append("mind_id = ?")
            params.append(mind_id)

        if not set_parts:
            return json.dumps({"updated": False, "error": "no fields provided to update"})

        set_clause = ", ".join(set_parts)
        params.append(int(memory_id))

        conn = _get_conn()
        cursor = conn.execute(
            f"UPDATE memories SET {set_clause} WHERE id = ?",
            params,
        )
        conn.commit()

        if cursor.rowcount == 0:
            return json.dumps({"updated": False, "error": "not found", "id": memory_id})

        # Get updated row details
        row = conn.execute(
            "SELECT data_class, mind_id, SUBSTR(content, 1, 80) AS preview FROM memories WHERE id = ?",
            (int(memory_id),),
        ).fetchone()

        return json.dumps({
            "updated": True,
            "id": int(memory_id),
            "data_class": row["data_class"] if row else data_class,
            "mind_id": row["mind_id"] if row else mind_id,
            "preview": row["preview"] if row else "",
        })
    except Exception as e:
        logger.exception("memory_update failed")
        return json.dumps({"error": str(e)})


def memory_retrieve(
    query: str,
    k: int = 10,
    mind_id: str | None = None,
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
        tag_filter: Optional tag to filter results.
        data_class: Optional data class filter — keep only entries with the
            given data_class.
        min_score: Optional cosine-similarity threshold — keep only entries
            with score >= min_score.

    Returns:
        JSON array of memories sorted by relevance (highest first).
    """
    k = min(k, 50)

    where_parts: list[str] = []
    params: list = []
    if tag_filter:
        where_parts.append("tags LIKE ?")
        params.append(f"%{tag_filter}%")
    if mind_id:
        where_parts.append("mind_id = ?")
        params.append(mind_id)
    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    try:
        query_embedding = np.array(_embed(query), dtype=np.float32)

        conn = _get_conn()
        rows = conn.execute(
            f"""
            SELECT id, content, embedding, tags, source, mind_id,
                   created_at, data_class, tier, as_of, expires_at,
                   superseded, codebase_ref
            FROM memories
            {where_clause}
            """,
            params,
        ).fetchall()

        if not rows:
            return json.dumps({"memories": [], "count": 0})

        # Compute cosine similarity
        scored_memories = []
        for row in rows:
            emb = _decode_embedding(row["embedding"])
            if emb is None:
                continue
            # Cosine similarity
            dot = np.dot(query_embedding, emb)
            norm_q = np.linalg.norm(query_embedding)
            norm_e = np.linalg.norm(emb)
            if norm_q == 0 or norm_e == 0:
                score = 0.0
            else:
                score = float(dot / (norm_q * norm_e))

            scored_memories.append({
                "content": row["content"],
                "tags": row["tags"],
                "source": row["source"],
                "mind_id": row["mind_id"],
                "created_at": row["created_at"],
                "score": round(score, 4),
                "data_class": row["data_class"],
                "tier": row["tier"],
                "as_of": row["as_of"],
                "expires_at": row["expires_at"],
                "superseded": bool(row["superseded"]) if row["superseded"] is not None else None,
                "codebase_ref": row["codebase_ref"],
            })

        # Sort by score descending; apply post-score filters; then top-k.
        scored_memories.sort(key=lambda m: m["score"], reverse=True)

        if data_class:
            scored_memories = [m for m in scored_memories if m.get("data_class") == data_class]
        if min_score is not None:
            scored_memories = [m for m in scored_memories if m["score"] >= min_score]

        top_k = scored_memories[:k]

        return json.dumps({"memories": top_k, "count": len(top_k)})
    except Exception as e:
        logger.exception("memory_retrieve failed")
        return json.dumps({"error": str(e)})


def query_decayed(limit: int = 20, mind_id: str | None = None) -> str:
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
    import math

    where_parts = ["tier = 'contextual'"]
    params: list = []
    if mind_id:
        where_parts.append("mind_id = ?")
        params.append(mind_id)
    where_clause = "WHERE " + " AND ".join(where_parts)

    try:
        conn = _get_conn()
        rows = conn.execute(
            f"""
            SELECT id, content, data_class, mind_id, created_at
            FROM memories
            {where_clause}
            """,
            params,
        ).fetchall()

        if not rows:
            return json.dumps([])

        now = time.time()
        half_life_seconds = DECAY_HALF_LIFE_DAYS * 86400
        scored = []
        for row in rows:
            created_at = row["created_at"] or 0
            score = math.exp(-(now - float(created_at)) / half_life_seconds)
            scored.append({
                "id": row["id"],
                "content": row["content"],
                "data_class": row["data_class"],
                "mind_id": row["mind_id"],
                "created_at": row["created_at"],
                "score": round(score, 4),
            })
        scored.sort(key=lambda r: r["score"], reverse=True)
        return json.dumps(scored[:limit])
    except Exception as e:
        logger.exception("query_decayed failed")
        return json.dumps({"error": str(e)})


HYBRID_RECENCY_HALF_LIFE_DAYS = 30
HYBRID_RECENCY_FLOOR = 0.5
HYBRID_VECTOR_POOL = 20
HYBRID_BM25_POOL = 10
HYBRID_DEFAULT_K = 5


_FTS_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "of", "in", "on", "at",
    "to", "for", "from", "by", "with", "as", "is", "are", "was", "were",
    "be", "been", "being", "do", "does", "did", "have", "has", "had",
    "this", "that", "these", "those", "it", "its", "i", "you", "we",
    "they", "he", "she", "me", "my", "our", "your", "their", "them",
    "us", "what", "when", "where", "why", "how", "who", "which",
    "can", "could", "would", "should", "will", "shall", "may", "might",
    "just", "really", "very", "so", "also", "too", "any", "some", "all",
    "no", "not", "yes", "ok", "okay",
}


def _sanitize_fts_query(query: str) -> str:
    """Strip FTS5 syntax characters and stopwords from a free-text query.

    FTS5 treats characters like quotes, hyphens, parens, colons, and
    asterisks as operators. We tokenize to alphanumerics, drop common
    English stopwords, then OR-join the rest so any meaningful token
    contributes to the BM25 ranking.
    """
    import re
    tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9_]+", query)]
    tokens = [t for t in tokens if len(t) > 2 and t not in _FTS_STOPWORDS]
    if not tokens:
        return ""
    return " OR ".join(tokens)


def memory_retrieve_hybrid(
    query: str,
    k: int = HYBRID_DEFAULT_K,
    mind_id: str | None = None,
    min_score: float | None = None,
    debug: bool = True,
) -> str:
    """Hybrid retrieval: vector + recency-weighted + BM25 keyword.

    Builds candidate pool from top-N vector neighbours unioned with top-M
    BM25 keyword matches. For each candidate computes three signals:
    cosine similarity, recency multiplier (exp decay, half-life
    ``HYBRID_RECENCY_HALF_LIFE_DAYS``, floor ``HYBRID_RECENCY_FLOOR``),
    and normalized BM25.

    Returns up to ``k`` rows in fixed buckets:
        - slot 1..3: top by cosine * recency  (label ``recency``)
        - slot 4:    top remaining by raw cosine  (label ``cosine``)
        - slot 5:    top remaining by BM25  (label ``bm25``)

    Empty slots are filled from a unified ranking. Each returned row
    includes a ``debug`` dict with ``bucket`` and component scores when
    ``debug=True``.
    """
    import math
    import re

    k = max(1, min(k, 20))
    now = time.time()
    half_life_seconds = HYBRID_RECENCY_HALF_LIFE_DAYS * 86400

    try:
        query_embedding = np.array(_embed(query), dtype=np.float32)
        qnorm = float(np.linalg.norm(query_embedding))
        if qnorm == 0:
            return json.dumps({"memories": [], "count": 0, "mode": "hybrid", "error": "zero_query_embedding"})

        conn = _get_conn()

        # ---- Step 1: pull every row's embedding (mind_id-aware) and score cosine ----
        where_parts: list[str] = []
        params: list = []
        if mind_id:
            where_parts.append("mind_id = ?")
            params.append(mind_id)
        where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

        rows = conn.execute(
            f"""
            SELECT id, content, embedding, tags, source, mind_id,
                   created_at, data_class, tier, as_of, expires_at,
                   superseded, codebase_ref
            FROM memories
            {where_clause}
            """,
            params,
        ).fetchall()
        if not rows:
            return json.dumps({"memories": [], "count": 0, "mode": "hybrid"})

        by_id: dict[int, dict] = {}
        cosine_scores: dict[int, float] = {}
        for row in rows:
            emb = _decode_embedding(row["embedding"])
            if emb is None:
                continue
            enorm = float(np.linalg.norm(emb))
            score = 0.0 if enorm == 0 else float(np.dot(query_embedding, emb) / (qnorm * enorm))
            rid = int(row["id"])
            cosine_scores[rid] = score
            by_id[rid] = {
                "id": rid,
                "content": row["content"],
                "tags": row["tags"],
                "source": row["source"],
                "mind_id": row["mind_id"],
                "created_at": row["created_at"],
                "data_class": row["data_class"],
                "tier": row["tier"],
                "as_of": row["as_of"],
                "expires_at": row["expires_at"],
                "superseded": bool(row["superseded"]) if row["superseded"] is not None else None,
                "codebase_ref": row["codebase_ref"],
            }

        # ---- Step 2: BM25 over FTS5 ----
        bm25_scores: dict[int, float] = {}
        fts_q = _sanitize_fts_query(query)
        if fts_q:
            try:
                fts_rows = conn.execute(
                    f"""
                    SELECT memories_fts.rowid AS rid, bm25(memories_fts) AS bm
                    FROM memories_fts
                    WHERE memories_fts MATCH ?
                    ORDER BY bm
                    LIMIT ?
                    """,
                    (fts_q, HYBRID_BM25_POOL * 4),
                ).fetchall()
                # bm25() returns negative; smaller (more negative) is better.
                # Normalize to a positive comparable score (higher is better).
                for r in fts_rows:
                    rid = int(r["rid"])
                    if rid in by_id:
                        bm25_scores[rid] = -float(r["bm"])
            except Exception:
                logger.exception("hybrid: bm25 lookup failed; continuing with vector-only")

        # ---- Step 3: candidate pool = top HYBRID_VECTOR_POOL by cosine UNION top HYBRID_BM25_POOL by bm25 ----
        top_cosine_ids = [rid for rid, _ in sorted(cosine_scores.items(), key=lambda kv: kv[1], reverse=True)[:HYBRID_VECTOR_POOL]]
        top_bm25_ids = [rid for rid, _ in sorted(bm25_scores.items(), key=lambda kv: kv[1], reverse=True)[:HYBRID_BM25_POOL]]
        pool_ids = list(dict.fromkeys(top_cosine_ids + top_bm25_ids))

        # ---- Step 4: per-candidate combined scoring ----
        def recency_mult(created_at) -> float:
            try:
                age = max(0.0, now - float(created_at or 0))
            except (TypeError, ValueError):
                return HYBRID_RECENCY_FLOOR
            raw = math.exp(-age / half_life_seconds)
            return max(HYBRID_RECENCY_FLOOR, raw)

        scored = []
        for rid in pool_ids:
            row = by_id[rid]
            cos = cosine_scores.get(rid, 0.0)
            rec = recency_mult(row.get("created_at"))
            bm = bm25_scores.get(rid, 0.0)
            if min_score is not None and cos < min_score and bm <= 0:
                # Skip dual-failure rows; allow BM25-positive rows through even at low cosine.
                continue
            scored.append({
                "rid": rid,
                "cos": cos,
                "rec": rec,
                "bm": bm,
                "row": row,
            })

        if not scored:
            return json.dumps({"memories": [], "count": 0, "mode": "hybrid"})

        # ---- Step 5: bucket allocation ----
        rec_slots = max(0, min(3, k - 2)) if k >= 5 else min(k, 3)
        cos_slots = 1 if k >= 4 else 0
        bm_slots = 1 if k >= 5 else 0
        # Ensure totals don't exceed k
        rec_slots = min(rec_slots, k)
        cos_slots = min(cos_slots, k - rec_slots)
        bm_slots = min(bm_slots, k - rec_slots - cos_slots)

        used: set[int] = set()
        picks: list[tuple[str, dict]] = []  # (bucket, scored_entry)

        # recency bucket
        rec_ranked = sorted(scored, key=lambda s: (s["cos"] * s["rec"]), reverse=True)
        for s in rec_ranked:
            if len(picks) >= rec_slots:
                break
            if s["rid"] in used:
                continue
            used.add(s["rid"])
            picks.append(("recency", s))

        # cosine bucket
        cos_ranked = sorted(scored, key=lambda s: s["cos"], reverse=True)
        cos_added = 0
        for s in cos_ranked:
            if cos_added >= cos_slots:
                break
            if s["rid"] in used:
                continue
            used.add(s["rid"])
            picks.append(("cosine", s))
            cos_added += 1

        # bm25 bucket
        bm_ranked = sorted(scored, key=lambda s: s["bm"], reverse=True)
        bm_added = 0
        for s in bm_ranked:
            if bm_added >= bm_slots:
                break
            if s["bm"] <= 0:
                break
            if s["rid"] in used:
                continue
            used.add(s["rid"])
            picks.append(("bm25", s))
            bm_added += 1

        # Backfill any leftover capacity from combined ranking
        if len(picks) < k:
            combined = sorted(
                scored,
                key=lambda s: (s["cos"] * s["rec"]) + (0.05 * s["bm"]),
                reverse=True,
            )
            for s in combined:
                if len(picks) >= k:
                    break
                if s["rid"] in used:
                    continue
                used.add(s["rid"])
                picks.append(("fill", s))

        # ---- Step 6: shape response ----
        out = []
        for bucket, s in picks:
            row = dict(s["row"])
            if debug:
                row["debug"] = {
                    "bucket": bucket,
                    "cosine": round(s["cos"], 4),
                    "recency_mult": round(s["rec"], 4),
                    "combined": round(s["cos"] * s["rec"], 4),
                    "bm25": round(s["bm"], 4),
                }
            row["score"] = round(s["cos"], 4)
            out.append(row)

        return json.dumps({
            "memories": out,
            "count": len(out),
            "mode": "hybrid",
            "pool_size": len(pool_ids),
        })
    except Exception as e:
        logger.exception("memory_retrieve_hybrid failed")
        return json.dumps({"error": str(e), "mode": "hybrid"})


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
