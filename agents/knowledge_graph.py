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
import logging
import os
import re
import time

import requests
from agent_tooling import tool
from agents.secret_manager import get_credential
from core.memory_schema import build_metadata, validate_source
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8420")
HITL_TTL = 180


def _hitl_gate(summary: str) -> bool:
    """Request HITL approval showing the exact graph operation to be written.

    Returns True if approved, False if denied or timed out.
    """
    try:
        resp = requests.post(
            f"{GATEWAY_URL}/hitl/request",
            json={"action": "graph_upsert", "summary": summary, "ttl": HITL_TTL},
            timeout=HITL_TTL + 5,
        )
        resp.raise_for_status()
        return resp.json().get("approved", False)
    except Exception:
        return False

NEO4J_URI = get_credential("NEO4J_URI") or "bolt://neo4j:7687"
NEO4J_AUTH_ENV = get_credential("NEO4J_AUTH") or "neo4j/hivemind-memory"
_NEO4J_USER, _, _NEO4J_PASS = NEO4J_AUTH_ENV.partition("/")

_driver = None
_kg_index_created = False

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


def _ensure_metadata_indexes(session) -> None:  # type: ignore[no-untyped-def]
    """Create property indexes for metadata fields on entity nodes.

    Uses a global guard to run only once per process, same pattern as
    agents/memory.py _ensure_index.
    """
    global _kg_index_created
    if _kg_index_created:
        return

    for label in _VALID_ENTITY_TYPES:
        for field in ("tier", "data_class", "source"):
            try:
                session.run(  # type: ignore[union-attr]
                    f"CREATE INDEX idx_{label.lower()}_{field} IF NOT EXISTS "
                    f"FOR (n:{label}) ON (n.{field})"
                )
            except Exception:
                logger.debug("Index idx_%s_%s may already exist", label.lower(), field)

    _kg_index_created = True


def graph_upsert_direct(
    *,
    entity_type: str,
    name: str,
    data_class: str,
    properties: str = "{}",
    relation: str = "",
    target_name: str = "",
    target_type: str = "",
    agent_id: str = "ada",
    as_of: str | None = None,
    source: str = "user",
) -> str:
    """Write to the knowledge graph without HITL. Called by the epilogue after batch approval."""
    try:
        # Validate data_class and source, build metadata
        try:
            validate_source(source)
        except ValueError as e:
            return json.dumps({"error": str(e)})

        try:
            meta = build_metadata(
                data_class=data_class, source=source, as_of=as_of
            )
        except ValueError as e:
            return json.dumps({"error": str(e)})

        label = _validate_label(entity_type)
        props = json.loads(properties) if properties.strip() != "{}" else {}

        # Merge metadata into props for the SET clause
        props.update(meta)

        # Add created_at timestamp for orphan sweep tracking
        props["created_at"] = time.time()

        driver = _get_driver()
        with driver.session() as session:
            _ensure_metadata_indexes(session)
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
                    SET t += $meta_props
                    WITH t
                    MATCH (n:{label} {{name: $name, agent_id: $agent_id}})
                    MERGE (n)-[r:{rel_type}]->(t)
                    SET r.as_of = $meta_as_of, r.source = $meta_source,
                        r.data_class = $meta_data_class, r.tier = $meta_tier
                    """,
                    target_name=target_name,
                    agent_id=agent_id,
                    name=name,
                    meta_props=meta,
                    meta_as_of=meta.get("as_of"),
                    meta_source=meta.get("source", source),
                    meta_data_class=meta.get("data_class"),
                    meta_tier=meta.get("tier"),
                )
                rel_created = True

        return json.dumps({
            "upserted": True,
            "id": node_id,
            "entity_type": label,
            "name": name,
            "relation_created": rel_created,
            "relation": f"-[:{relation}]->({target_name})" if rel_created else None,
            "data_class": meta.get("data_class"),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool(tags=["memory"])
def graph_upsert(
    *,
    entity_type: str,
    name: str,
    data_class: str,
    properties: str = "{}",
    relation: str = "",
    target_name: str = "",
    target_type: str = "",
    agent_id: str = "ada",
    as_of: str | None = None,
    source: str = "user",
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
        data_class: Data class for this entry (e.g. "person", "preference", "technical-config").
        as_of: ISO datetime for when the fact was established (defaults to now).
        source: Origin of the entry — "user", "tool", "session", or "self".

    Returns:
        JSON confirmation with node id and relationship created (if any).
    """
    try:
        from core.kg_guards import check_disambiguation, check_orphan_guard, send_disambiguation_message

        label = _validate_label(entity_type)
        props = json.loads(properties) if properties.strip() != "{}" else {}

        # Orphan guard: reject writes without edges
        allowed, orphan_msg = check_orphan_guard(relation, target_name)
        if not allowed:
            return json.dumps({"upserted": False, "reason": orphan_msg})

        # Disambiguation: check for similar/duplicate nodes
        disambig = check_disambiguation(name, entity_type, agent_id)
        if disambig.action == "disambiguate":
            send_disambiguation_message(name, disambig.existing_nodes)
            return json.dumps({
                "upserted": False,
                "reason": "disambiguation_required",
                "similar_nodes": disambig.existing_nodes,
            })

        # Build HITL summary showing the exact graph operation
        props_str = f" {json.dumps(props)}" if props else ""
        node_repr = f"({label}:{name}{props_str})"
        if relation and target_name:
            tgt = target_type or entity_type
            hitl_summary = f"{node_repr} --[{relation}]--> ({tgt}:{target_name})"
        else:
            hitl_summary = node_repr

        if not _hitl_gate(hitl_summary):
            return json.dumps({"upserted": False, "reason": "denied by HITL"})

        return graph_upsert_direct(
            entity_type=entity_type,
            name=name,
            properties=properties,
            relation=relation,
            target_name=target_name,
            target_type=target_type,
            agent_id=agent_id,
            data_class=data_class,
            as_of=as_of,
            source=source,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool(tags=["memory"])
def graph_query(
    entity_name: str,
    agent_id: str = "ada",
    depth: int = 1,
) -> str:
    """Retrieve knowledge graph node(s) and their connected relationships.

    Searches by full name (exact), first_name, last_name, or aliases.
    Returns all matching nodes — if multiple match, disambiguate before acting.

    Args:
        entity_name: Name or name fragment to search (e.g. "Wil", "Vark", "Wil Vark", "Coach").
        agent_id: Which agent's graph to search (default "ada").
        depth: How many hops to traverse (default 1, max 3).

    Returns:
        JSON with matching nodes, their properties, and connected relationships.
    """
    depth = min(max(depth, 1), 3)
    try:
        driver = _get_driver()
        with driver.session() as session:
            result = session.run(
                f"""
                MATCH (n {{agent_id: $agent_id}})
                WHERE n.name = $query
                   OR n.first_name = $query
                   OR n.last_name = $query
                   OR $query IN coalesce(n.aliases, [])
                WITH n
                OPTIONAL MATCH (n)-[r*1..{depth}]-(m)
                RETURN n,
                       [rel IN r | {{type: type(rel), direction: 'out'}}] AS rels,
                       m
                """,
                query=entity_name,
                agent_id=agent_id,
            )
            rows = result.data()

        if not rows:
            return json.dumps({"found": False, "entity": entity_name})

        nodes: dict[str, dict] = {}
        for row in rows:
            node = dict(row["n"])
            key = node.get("name", str(node))
            if key not in nodes:
                nodes[key] = {"properties": node, "connections": []}
            if row["m"]:
                nodes[key]["connections"].append({
                    "node": dict(row["m"]),
                    "via": row["rels"],
                })

        matches = list(nodes.values())
        return json.dumps({
            "found": True,
            "count": len(matches),
            "matches": matches,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool(tags=["memory"])
def search_person(
    first_name: str = "",
    last_name: str = "",
    title: str = "",
    relationship: str = "",
    agent_id: str = "ada",
) -> str:
    """Search for Person nodes by any known name fragment or relationship to Daniel.

    All parameters are optional but at least one must be provided.
    Matching is case-insensitive substring (CONTAINS) — pass whatever you know.
    Multiple params are combined with AND (all must match).

    Args:
        first_name: Given name fragment (e.g. "Wil", "manny").
        last_name: Surname fragment (e.g. "Vark", "stew").
        title: Title or honorific fragment (e.g. "Coach", "Dr").
        relationship: How this person relates to Daniel (e.g. "wife", "doctor", "child").
        agent_id: Which agent's graph to search (default "ada").

    Returns:
        JSON with matching Person nodes and their properties.
    """
    if not any([first_name, last_name, title, relationship]):
        return json.dumps({"error": "At least one search parameter must be provided."})
    try:
        driver = _get_driver()
        with driver.session() as session:
            result = session.run(
                """
                MATCH (n:Person {agent_id: $agent_id})
                WHERE ($first_name = '' OR toLower(coalesce(n.first_name, '')) CONTAINS toLower($first_name))
                  AND ($last_name = '' OR toLower(coalesce(n.last_name, '')) CONTAINS toLower($last_name))
                  AND ($title = '' OR toLower(coalesce(n.title, '')) CONTAINS toLower($title))
                  AND ($relationship = '' OR ANY(r IN coalesce(n.relationship, []) WHERE toLower(r) CONTAINS toLower($relationship)))
                RETURN n
                """,
                first_name=first_name,
                last_name=last_name,
                title=title,
                relationship=relationship,
                agent_id=agent_id,
            )
            rows = result.data()

        if not rows:
            return json.dumps({"found": False, "query": {
                "first_name": first_name,
                "last_name": last_name,
                "title": title,
                "relationship": relationship,
            }})

        matches = [dict(row["n"]) for row in rows]
        return json.dumps({
            "found": True,
            "count": len(matches),
            "matches": matches,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})
