"""Semantic memory tools for Ada — vector-backed persistent memory via Neo4j.

Stores experiences, observations, and session summaries as embeddings.
Retrieves the most relevant memories by semantic similarity.

Model: qwen3-embedding:8b via Ollama (4096-dim)
Backend: Neo4j with native vector index (cosine similarity)

Required env vars:
  NEO4J_URI      — bolt://neo4j:7687 (default)
  NEO4J_AUTH     — user/password (default: neo4j/hivemind-memory)
  OLLAMA_BASE_URL — http://203.0.113.10:11434 (default)
"""

import json
import logging
import os
import time
from typing import Optional

import requests
from agent_tooling import tool
from agents.secret_manager import get_credential
from core.memory_schema import build_metadata, validate_source
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8420")
HITL_TTL = 180


def _hitl_gate(content: str) -> bool:
    """Request HITL approval showing the exact content to be stored.

    Returns True if approved, False if denied or timed out.
    """
    try:
        resp = requests.post(
            f"{GATEWAY_URL}/hitl/request",
            json={"action": "memory_store", "summary": content, "ttl": HITL_TTL},
            timeout=HITL_TTL + 5,
        )
        resp.raise_for_status()
        return resp.json().get("approved", False)
    except Exception:
        logger.exception("HITL gate failed — denying memory write by default")
        return False

# --- Lazy singletons ---
_driver = None
_index_created = False

NEO4J_URI = get_credential("NEO4J_URI") or "bolt://neo4j:7687"
NEO4J_AUTH_ENV = get_credential("NEO4J_AUTH") or "neo4j/hivemind-memory"
_NEO4J_USER, _, _NEO4J_PASS = NEO4J_AUTH_ENV.partition("/")

OLLAMA_BASE_URL = get_credential("OLLAMA_BASE_URL") or "http://203.0.113.10:11434"
EMBEDDING_MODEL = "qwen3-embedding:8b"
EMBEDDING_DIM = 4096
VECTOR_INDEX = "memory_embedding"


def _get_driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(NEO4J_URI, auth=(_NEO4J_USER, _NEO4J_PASS))
    return _driver


def _ensure_index(session):
    global _index_created
    if _index_created:
        return
    session.run(
        """
        CREATE VECTOR INDEX memory_embedding IF NOT EXISTS
        FOR (m:Memory) ON (m.embedding)
        OPTIONS {indexConfig: {
            `vector.dimensions`: 4096,
            `vector.similarity_function`: 'cosine'
        }}
        """
    )
    # Create property indexes for metadata fields on Memory nodes
    for field in ("tier", "data_class", "expires_at", "source"):
        try:
            session.run(
                f"CREATE INDEX idx_memory_{field} IF NOT EXISTS "
                f"FOR (m:Memory) ON (m.{field})"
            )
        except Exception:
            logger.debug("Index idx_memory_%s may already exist", field)
    _index_created = True


def _embed(text: str) -> list[float]:
    resp = requests.post(
        f"{OLLAMA_BASE_URL}/api/embed",
        json={"model": EMBEDDING_MODEL, "input": text},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["embeddings"][0]


def memory_store_direct(
    *,
    content: str,
    data_class: str,
    tags: str = "",
    source: str = "user",
    agent_id: str = "ada",
    as_of: str | None = None,
    expires_at: str | None = None,
) -> str:
    """Write to vector memory without HITL. Called by the epilogue after batch approval."""
    try:
        # Validate data_class and source, build metadata
        try:
            validate_source(source)
        except ValueError as e:
            return json.dumps({"stored": False, "error": str(e)})

        try:
            meta = build_metadata(
                data_class=data_class, source=source, as_of=as_of, expires_at=expires_at
            )
        except ValueError as e:
            return json.dumps({"stored": False, "error": str(e), "prompt": str(e)})

        embedding = _embed(content)
        driver = _get_driver()
        with driver.session() as session:
            _ensure_index(session)
            result = session.run(
                """
                CREATE (m:Memory {
                    content: $content,
                    tags: $tags,
                    source: $source,
                    agent_id: $agent_id,
                    created_at: $created_at,
                    embedding: $embedding,
                    data_class: $data_class,
                    tier: $tier,
                    as_of: $as_of,
                    expires_at: $expires_at,
                    superseded: $superseded
                })
                RETURN elementId(m) AS id
                """,
                content=content,
                tags=tags,
                source=meta.get("source", source),
                agent_id=agent_id,
                created_at=int(time.time()),
                embedding=embedding,
                data_class=meta.get("data_class"),
                tier=meta.get("tier"),
                as_of=meta.get("as_of"),
                expires_at=meta.get("expires_at"),
                superseded=meta.get("superseded", False),
            )
            record = result.single()
            memory_id = record["id"] if record else "unknown"
        return json.dumps({
            "stored": True,
            "id": memory_id,
            "agent_id": agent_id,
            "data_class": meta.get("data_class"),
        })
    except Exception as e:
        logger.exception("memory_store_direct failed")
        return json.dumps({"error": str(e)})


@tool(tags=["memory"])
def memory_store(
    *,
    content: str,
    data_class: str,
    tags: str = "",
    source: str = "user",
    agent_id: str = "ada",
    as_of: str | None = None,
    expires_at: str | None = None,
) -> str:
    """Store a memory as a semantic embedding in Neo4j.

    Args:
        content: The text to remember (experience, observation, fact, etc.)
        data_class: Memory data class (e.g. "person", "preference", "technical-config"). Required.
        tags: Comma-separated tags for categorisation (e.g. "session,preference")
        source: Origin of the memory -- "user", "tool", "session", "self"
        agent_id: Which agent this memory belongs to (default "ada")
        as_of: ISO datetime for when the fact was established (defaults to now)
        expires_at: ISO datetime for when a timed-event expires (required for timed-event)

    Returns:
        JSON with the stored memory ID and confirmation.
    """
    try:
        if not _hitl_gate(content):
            return json.dumps({"stored": False, "reason": "denied by HITL"})

        return memory_store_direct(
            content=content,
            tags=tags,
            source=source,
            agent_id=agent_id,
            data_class=data_class,
            as_of=as_of,
            expires_at=expires_at,
        )
    except Exception as e:
        logger.exception("memory_store failed")
        return json.dumps({"error": str(e)})


@tool(tags=["memory"])
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
        tag_filter: Optional tag to filter results (e.g. "session").

    Returns:
        JSON array of memories sorted by relevance (highest first), each with
        content, tags, source, agent_id, created_at, and similarity score.
    """
    k = min(k, 50)
    try:
        embedding = _embed(query)
        driver = _get_driver()
        with driver.session() as session:
            _ensure_index(session)
            if tag_filter:
                result = session.run(
                    """
                    CALL db.index.vector.queryNodes($index, $k, $embedding)
                    YIELD node AS m, score
                    WHERE m.agent_id = $agent_id AND m.tags CONTAINS $tag_filter
                    RETURN m.content AS content,
                           m.tags AS tags,
                           m.source AS source,
                           m.agent_id AS agent_id,
                           m.created_at AS created_at,
                           m.data_class AS data_class,
                           m.tier AS tier,
                           m.as_of AS as_of,
                           m.expires_at AS expires_at,
                           m.superseded AS superseded,
                           score
                    ORDER BY score DESC
                    """,
                    index=VECTOR_INDEX,
                    k=k,
                    embedding=embedding,
                    agent_id=agent_id,
                    tag_filter=tag_filter,
                )
            else:
                result = session.run(
                    """
                    CALL db.index.vector.queryNodes($index, $k, $embedding)
                    YIELD node AS m, score
                    WHERE m.agent_id = $agent_id
                    RETURN m.content AS content,
                           m.tags AS tags,
                           m.source AS source,
                           m.agent_id AS agent_id,
                           m.created_at AS created_at,
                           m.data_class AS data_class,
                           m.tier AS tier,
                           m.as_of AS as_of,
                           m.expires_at AS expires_at,
                           m.superseded AS superseded,
                           score
                    ORDER BY score DESC
                    """,
                    index=VECTOR_INDEX,
                    k=k,
                    embedding=embedding,
                    agent_id=agent_id,
                )
            memories = [
                {
                    "content": record["content"],
                    "tags": record["tags"],
                    "source": record["source"],
                    "agent_id": record["agent_id"],
                    "created_at": record["created_at"],
                    "score": round(record["score"], 4),
                    "data_class": record["data_class"],
                    "tier": record["tier"],
                    "as_of": record["as_of"],
                    "expires_at": record["expires_at"],
                    "superseded": record["superseded"],
                }
                for record in result
            ]
        return json.dumps({"memories": memories, "count": len(memories)})
    except Exception as e:
        logger.exception("memory_retrieve failed")
        return json.dumps({"error": str(e)})
