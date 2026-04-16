#!/usr/bin/env python3
"""One-time migration script: Neo4j -> Lucent (SQLite).

Reads all nodes, edges, and memories from Neo4j and writes them to
the Lucent SQLite database (data/lucent.db).

Usage:
    python tools/stateless/lucent_migrate.py [--dry-run]
"""

import argparse
import json
import logging
import time

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def migrate(dry_run: bool = False) -> dict:
    """Run the full Neo4j -> Lucent migration.

    Args:
        dry_run: If True, read from Neo4j but don't write to SQLite.

    Returns:
        Summary dict with nodes, edges, memories counts.
    """
    from core.secrets import get_credential
    from neo4j import GraphDatabase
    from tools.stateful.lucent import _get_connection

    # Connect to Neo4j
    neo4j_uri = get_credential("NEO4J_URI") or "bolt://neo4j:7687"
    neo4j_auth_env = get_credential("NEO4J_AUTH") or "neo4j/hivemind-memory"
    user, _, password = neo4j_auth_env.partition("/")

    driver = GraphDatabase.driver(neo4j_uri, auth=(user, password))

    # Connect to SQLite
    conn = _get_connection()

    # --- Migrate nodes ---
    node_count = 0
    neo4j_id_to_sqlite_id: dict[str, int] = {}

    with driver.session() as session:
        result = session.run(
            "MATCH (n) WHERE n.agent_id IS NOT NULL "
            "RETURN n, labels(n) AS labels, elementId(n) AS eid"
        )
        for record in result:
            node = dict(record["n"])
            labels = record["labels"]
            eid = record["eid"]

            agent_id = node.pop("agent_id", "ada")
            name = node.pop("name", "")
            first_name = node.pop("first_name", None)
            last_name = node.pop("last_name", None)
            data_class = node.pop("data_class", None)
            tier = node.pop("tier", None)
            source = node.pop("source", None)
            as_of = node.pop("as_of", None)
            created_at = node.pop("created_at", time.time())
            updated_at = node.pop("updated_at", created_at)
            node.pop("embedding", None)  # Remove embedding from properties

            # Use first non-internal label as type
            node_type = next(
                (lbl for lbl in labels if lbl not in ("Memory",)), labels[0] if labels else "Concept"
            )

            if not dry_run:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO nodes
                        (agent_id, type, name, first_name, last_name,
                         properties, data_class, tier, source, as_of,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        agent_id, node_type, name, first_name, last_name,
                        json.dumps(node), data_class, tier, source, as_of,
                        created_at, updated_at,
                    ),
                )
                row = conn.execute(
                    "SELECT id FROM nodes WHERE agent_id = ? AND name = ?",
                    (agent_id, name),
                ).fetchone()
                if row:
                    neo4j_id_to_sqlite_id[eid] = row["id"]

            node_count += 1
            if node_count % 100 == 0:
                logger.info("  Nodes: %d...", node_count)

    if not dry_run:
        conn.commit()
    logger.info("Nodes exported: %d", node_count)

    # --- Migrate edges ---
    edge_count = 0

    with driver.session() as session:
        result = session.run(
            "MATCH (a)-[r]->(b) "
            "RETURN elementId(a) AS src_eid, elementId(b) AS tgt_eid, "
            "       type(r) AS type, r AS props"
        )
        for record in result:
            src_eid = record["src_eid"]
            tgt_eid = record["tgt_eid"]
            rel_type = record["type"]
            rel_props = dict(record["props"]) if record["props"] else {}

            src_id = neo4j_id_to_sqlite_id.get(src_eid)
            tgt_id = neo4j_id_to_sqlite_id.get(tgt_eid)

            if src_id is None or tgt_id is None:
                logger.warning("Skipping edge %s->%s: missing node mapping", src_eid, tgt_eid)
                continue

            if not dry_run:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO edges
                        (agent_id, source_id, target_id, type,
                         as_of, source, data_class, tier, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rel_props.get("agent_id", "ada"),
                        src_id, tgt_id, rel_type,
                        rel_props.get("as_of"),
                        rel_props.get("source"),
                        rel_props.get("data_class"),
                        rel_props.get("tier"),
                        rel_props.get("created_at", time.time()),
                    ),
                )

            edge_count += 1

    if not dry_run:
        conn.commit()
    logger.info("Edges exported: %d", edge_count)

    # --- Migrate memories ---
    memory_count = 0

    with driver.session() as session:
        result = session.run(
            "MATCH (m:Memory) RETURN m, elementId(m) AS eid"
        )
        for record in result:
            mem = dict(record["m"])

            agent_id = mem.get("agent_id", "ada")
            content = mem.get("content", "")
            embedding_list = mem.get("embedding")
            tags = mem.get("tags", "")
            source = mem.get("source")
            data_class = mem.get("data_class")
            tier = mem.get("tier")
            as_of = mem.get("as_of")
            expires_at = mem.get("expires_at")
            superseded = 1 if mem.get("superseded") else 0
            recurring = mem.get("recurring")
            if recurring is not None:
                recurring = 1 if recurring else 0
            codebase_ref = mem.get("codebase_ref")
            created_at = mem.get("created_at", int(time.time()))

            # Convert embedding to numpy blob
            embedding_blob = None
            if embedding_list and isinstance(embedding_list, list):
                embedding_blob = np.array(embedding_list, dtype=np.float32).tobytes()

            if not dry_run:
                conn.execute(
                    """
                    INSERT INTO memories
                        (agent_id, content, embedding, tags, source,
                         data_class, tier, as_of, expires_at,
                         superseded, recurring, codebase_ref, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        agent_id, content, embedding_blob, tags, source,
                        data_class, tier, as_of, expires_at,
                        superseded, recurring, codebase_ref, created_at,
                    ),
                )

            memory_count += 1
            if memory_count % 100 == 0:
                logger.info("  Memories: %d...", memory_count)

    if not dry_run:
        conn.commit()
    logger.info("Memories exported: %d", memory_count)

    # --- Validate ---
    if not dry_run:
        sqlite_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        sqlite_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        sqlite_memories = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        logger.info(
            "Validation: nodes=%d/%d, edges=%d/%d, memories=%d/%d",
            sqlite_nodes, node_count, sqlite_edges, edge_count,
            sqlite_memories, memory_count,
        )
    else:
        logger.info("Dry run -- no data written.")

    driver.close()
    return {"nodes": node_count, "edges": edge_count, "memories": memory_count}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate Neo4j data to Lucent (SQLite)")
    parser.add_argument("--dry-run", action="store_true", help="Read from Neo4j without writing")
    args = parser.parse_args()
    summary = migrate(dry_run=args.dry_run)
    print(json.dumps(summary))
