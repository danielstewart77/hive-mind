"""Neo4j knowledge graph tools for Ada — structured entity and relationship storage.

Complements vector memory (episodic) with relational knowledge:
who/what/how things connect.

Node types: Person, Project, System, Concept, Preference
Standard relationships: KNOWS_ABOUT, WORKS_ON, PREFERS, RELATED_TO, MANAGES

Required env vars (shared with memory.py):
  NEO4J_URI  — bolt://neo4j:7687 (default)
  NEO4J_AUTH — user/password (default: neo4j/hivemind-memory)
"""

import json
import os
import re

from agent_tooling import tool
from agents.secret_manager import get_credential
from neo4j import GraphDatabase

NEO4J_URI = get_credential("NEO4J_URI") or "bolt://neo4j:7687"
NEO4J_AUTH_ENV = get_credential("NEO4J_AUTH") or "neo4j/hivemind-memory"
_NEO4J_USER, _, _NEO4J_PASS = NEO4J_AUTH_ENV.partition("/")

_driver = None

_VALID_ENTITY_TYPES = {"Person", "Project", "System", "Concept", "Preference"}
_VALID_RELATIONS = {"KNOWS_ABOUT", "WORKS_ON", "PREFERS", "RELATED_TO", "MANAGES"}
# Allow any uppercase/underscore relation beyond the standard set
_RELATION_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _get_driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(NEO4J_URI, auth=(_NEO4J_USER, _NEO4J_PASS))
    return _driver


def _validate_label(entity_type: str) -> str:
    if entity_type not in _VALID_ENTITY_TYPES:
        raise ValueError(
            f"Invalid entity_type {entity_type!r}. Must be one of: {sorted(_VALID_ENTITY_TYPES)}"
        )
    return entity_type


def _validate_relation(relation: str) -> str:
    if not _RELATION_RE.match(relation):
        raise ValueError(
            f"Invalid relation {relation!r}. Must be uppercase letters/underscores."
        )
    return relation


@tool(tags=["memory"])
def graph_upsert(
    entity_type: str,
    name: str,
    properties: str = "{}",
    relation: str = "",
    target_name: str = "",
    target_type: str = "",
    agent_id: str = "ada",
) -> str:
    """Add or update a knowledge graph node, optionally linking it to another node.

    Args:
        entity_type: Node label — one of Person, Project, System, Concept, Preference.
        name: Unique name for this entity (e.g. "Daniel", "Hive Mind").
        properties: JSON string of extra properties to set (e.g. '{"role": "owner"}').
        relation: Relationship type to create (e.g. MANAGES, WORKS_ON). Leave empty for node-only.
        target_name: Name of the target node to link to. Required if relation is set.
        target_type: Entity type of the target node (defaults to entity_type if omitted).
        agent_id: Which agent's graph this belongs to (default "ada").

    Returns:
        JSON confirmation with node id and relationship created (if any).
    """
    try:
        label = _validate_label(entity_type)
        props = json.loads(properties) if properties.strip() != "{}" else {}

        driver = _get_driver()
        with driver.session() as session:
            # Upsert the node
            result = session.run(
                f"""
                MERGE (n:{label} {{name: $name, agent_id: $agent_id}})
                SET n += $props
                RETURN elementId(n) AS id
                """,
                name=name,
                agent_id=agent_id,
                props=props,
            )
            node_id = result.single()["id"]

            rel_created = False
            if relation and target_name:
                rel_type = _validate_relation(relation)
                tgt_label = _validate_label(target_type or entity_type)
                session.run(
                    f"""
                    MERGE (t:{tgt_label} {{name: $target_name, agent_id: $agent_id}})
                    WITH t
                    MATCH (n:{label} {{name: $name, agent_id: $agent_id}})
                    MERGE (n)-[:{rel_type}]->(t)
                    """,
                    target_name=target_name,
                    agent_id=agent_id,
                    name=name,
                )
                rel_created = True

        return json.dumps({
            "upserted": True,
            "id": node_id,
            "entity_type": label,
            "name": name,
            "relation_created": rel_created,
            "relation": f"-[:{relation}]->({target_name})" if rel_created else None,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool(tags=["memory"])
def graph_query(
    entity_name: str,
    agent_id: str = "ada",
    depth: int = 1,
) -> str:
    """Retrieve a knowledge graph node and its connected relationships.

    Args:
        entity_name: Name of the entity to look up (e.g. "Daniel").
        agent_id: Which agent's graph to search (default "ada").
        depth: How many hops to traverse (default 1, max 3).

    Returns:
        JSON with the node's properties and all connected nodes/relationships.
    """
    depth = min(max(depth, 1), 3)
    try:
        driver = _get_driver()
        with driver.session() as session:
            result = session.run(
                f"""
                MATCH (n {{name: $name, agent_id: $agent_id}})
                OPTIONAL MATCH (n)-[r*1..{depth}]-(m)
                RETURN n,
                       [rel IN r | {{type: type(rel), direction: 'out'}}] AS rels,
                       m
                """,
                name=entity_name,
                agent_id=agent_id,
            )
            rows = result.data()

        if not rows:
            return json.dumps({"found": False, "entity": entity_name})

        node_props = dict(rows[0]["n"])
        connections = []
        for row in rows:
            if row["m"]:
                connections.append({
                    "node": dict(row["m"]),
                    "via": row["rels"],
                })

        return json.dumps({
            "found": True,
            "entity": node_props,
            "connections": connections,
            "connection_count": len(connections),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})
