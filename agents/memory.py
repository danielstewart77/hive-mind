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
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

# --- Lazy singletons ---
_driver = None
_index_created = False

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://neo4j:7687")
NEO4J_AUTH_ENV = os.getenv("NEO4J_AUTH", "neo4j/hivemind-memory")
_NEO4J_USER, _, _NEO4J_PASS = NEO4J_AUTH_ENV.partition("/")

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://203.0.113.10:11434")
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
    _index_created = True


def _embed(text: str) -> list[float]:
    resp = requests.post(
        f"{OLLAMA_BASE_URL}/api/embed",
        json={"model": EMBEDDING_MODEL, "input": text},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["embeddings"][0]


@tool(tags=["memory"])
def memory_store(
    content: str,
    tags: str = "",
    source: str = "user",
    agent_id: str = "ada",
) -> str:
    """Store a memory as a semantic embedding in Neo4j.

    Args:
        content: The text to remember (experience, observation, fact, etc.)
        tags: Comma-separated tags for categorisation (e.g. "session,preference")
        source: Origin of the memory — "user", "tool", "session", "self"
        agent_id: Which agent this memory belongs to (default "ada")

    Returns:
        JSON with the stored memory ID and confirmation.
    """
    try:
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
                    embedding: $embedding
                })
                RETURN elementId(m) AS id
                """,
                content=content,
                tags=tags,
                source=source,
                agent_id=agent_id,
                created_at=int(time.time()),
                embedding=embedding,
            )
            record = result.single()
            memory_id = record["id"] if record else "unknown"
        return json.dumps({"stored": True, "id": memory_id, "agent_id": agent_id})
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
                }
                for record in result
            ]
        return json.dumps({"memories": memories, "count": len(memories)})
    except Exception as e:
        logger.exception("memory_retrieve failed")
        return json.dumps({"error": str(e)})
