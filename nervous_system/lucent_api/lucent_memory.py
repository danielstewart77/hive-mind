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
from core.memory_schema import build_metadata, validate_source
from core.secrets import get_credential

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8420")
HITL_TTL = 180

OLLAMA_BASE_URL = get_credential("OLLAMA_BASE_URL") or "http://203.0.113.10:11434"
EMBEDDING_MODEL = "qwen3-embedding:8b"
EMBEDDING_DIM = 4096


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
    from nervous_system.lucent_api.lucent import _get_connection
    return _get_connection()



def memory_store_direct(
    *,
    content: str,
    data_class: str,
    tags: str = "",
    source: str = "user",
    agent_id: str = "ada",
    as_of: str | None = None,
    expires_at: str | None = None,
    recurring: bool | None = None,
    codebase_ref: str | None = None,
) -> str:
    """Write to vector memory without HITL. Called by the epilogue after batch approval."""
    try:
        try:
            validate_source(source)
        except ValueError as e:
            return json.dumps({"stored": False, "error": str(e)})

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
        embedding_blob = np.array(embedding, dtype=np.float32).tobytes()

        conn = _get_conn()
        cursor = conn.execute(
            """
            INSERT INTO memories (
                agent_id, content, embedding, tags, source,
                data_class, tier, as_of, expires_at,
                superseded, recurring, codebase_ref, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent_id, content, embedding_blob, tags,
                meta.get("source", source),
                meta.get("data_class"), meta.get("tier"), meta.get("as_of"),
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
            "agent_id": agent_id,
            "data_class": meta.get("data_class"),
        })
    except Exception as e:
        logger.exception("memory_store_direct failed")
        return json.dumps({"error": str(e)})


def memory_store(
    *,
    content: str,
    data_class: str,
    tags: str = "",
    source: str = "user",
    agent_id: str = "ada",
    as_of: str | None = None,
    expires_at: str | None = None,
    recurring: bool | None = None,
    codebase_ref: str | None = None,
) -> str:
    """Store a memory as a semantic embedding.

    Args:
        content: The text to remember.
        data_class: Memory data class (e.g. "person", "preference"). Required.
        tags: Comma-separated tags for categorisation.
        source: Origin of the memory -- "user", "tool", "session", "self".
        agent_id: Which agent this memory belongs to (default "ada").
        as_of: ISO datetime for when the fact was established.
        expires_at: ISO datetime for when a timed-event expires.
        recurring: Explicit recurring flag for timed-events.
        codebase_ref: Optional file path or symbol reference.

    Returns:
        JSON with the stored memory ID and confirmation.
    """
    try:
        return memory_store_direct(
            content=content,
            tags=tags,
            source=source,
            agent_id=agent_id,
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
    agent_id: str = "ada",
) -> str:
    """List all memories sequentially by creation time for review and cleanup.

    Args:
        offset: Number of entries to skip (for pagination).
        limit: Number of entries to return (default 25, max 100).
        agent_id: Which agent's memories to list (default "ada").

    Returns:
        JSON with entries, offset, limit, and total count.
    """
    limit = min(limit, 100)
    try:
        conn = _get_conn()
        total_row = conn.execute(
            "SELECT COUNT(*) AS total FROM memories WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        total = total_row["total"]

        rows = conn.execute(
            """
            SELECT id, content, tags, source, data_class, created_at
            FROM memories
            WHERE agent_id = ?
            ORDER BY created_at ASC
            LIMIT ? OFFSET ?
            """,
            (agent_id, limit, offset),
        ).fetchall()

        entries = [
            {
                "id": row["id"],
                "content": row["content"],
                "tags": row["tags"],
                "source": row["source"],
                "data_class": row["data_class"],
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
            "SELECT id, content, agent_id FROM memories WHERE id = ?",
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
) -> str:
    """Update an existing memory.

    Args:
        memory_id: The ID of the memory to update.
        content: New content to store. Re-embeds automatically.
        data_class: New data class to assign.
        tags: Comma-separated tags to replace existing tags.

    Returns:
        JSON with updated memory details or error.
    """
    from core.memory_schema import DATA_CLASS_REGISTRY, validate_data_class

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
            "SELECT data_class, SUBSTR(content, 1, 80) AS preview FROM memories WHERE id = ?",
            (int(memory_id),),
        ).fetchone()

        return json.dumps({
            "updated": True,
            "id": int(memory_id),
            "data_class": row["data_class"] if row else data_class,
            "preview": row["preview"] if row else "",
        })
    except Exception as e:
        logger.exception("memory_update failed")
        return json.dumps({"error": str(e)})


def memory_retrieve(
    query: str,
    k: int = 10,
    agent_id: str = "ada",
    tag_filter: Optional[str] = None,
) -> str:
    """Retrieve the most semantically relevant memories for a query.

    Args:
        query: Natural language query to search for related memories.
        k: Number of results to return (default 10, max 50).
        agent_id: Which agent's memories to search (default "ada").
        tag_filter: Optional tag to filter results.

    Returns:
        JSON array of memories sorted by relevance (highest first).
    """
    k = min(k, 50)
    try:
        query_embedding = np.array(_embed(query), dtype=np.float32)

        conn = _get_conn()

        # Build query with optional tag filter
        if tag_filter:
            rows = conn.execute(
                """
                SELECT id, content, embedding, tags, source, agent_id,
                       created_at, data_class, tier, as_of, expires_at,
                       superseded, codebase_ref
                FROM memories
                WHERE agent_id = ? AND tags LIKE ?
                """,
                (agent_id, f"%{tag_filter}%"),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, content, embedding, tags, source, agent_id,
                       created_at, data_class, tier, as_of, expires_at,
                       superseded, codebase_ref
                FROM memories
                WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchall()

        if not rows:
            return json.dumps({"memories": [], "count": 0})

        # Compute cosine similarity
        scored_memories = []
        for row in rows:
            if row["embedding"] is None:
                continue
            emb = np.frombuffer(row["embedding"], dtype=np.float32)
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
                "agent_id": row["agent_id"],
                "created_at": row["created_at"],
                "score": round(score, 4),
                "data_class": row["data_class"],
                "tier": row["tier"],
                "as_of": row["as_of"],
                "expires_at": row["expires_at"],
                "superseded": bool(row["superseded"]) if row["superseded"] is not None else None,
                "codebase_ref": row["codebase_ref"],
            })

        # Sort by score descending and take top-k
        scored_memories.sort(key=lambda m: m["score"], reverse=True)
        top_k = scored_memories[:k]

        return json.dumps({"memories": top_k, "count": len(top_k)})
    except Exception as e:
        logger.exception("memory_retrieve failed")
        return json.dumps({"error": str(e)})


# All memory tool functions for registration
MEMORY_TOOLS = [
    memory_store,
    memory_store_direct,
    memory_list,
    memory_delete,
    memory_update,
    memory_retrieve,
]
