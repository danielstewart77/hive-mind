"""Lucent knowledge graph tools -- SQLite-backed drop-in replacement for knowledge_graph.py.

Provides identical function signatures and JSON return shapes to
tools/stateful/knowledge_graph.py, backed by the Lucent SQLite database
instead of Neo4j.

Node types: Person, Project, System, Concept, Preference
Standard relationships: KNOWS_ABOUT, WORKS_ON, PREFERS, RELATED_TO, MANAGES

Designed for direct FastMCP registration (no @tool() decorator).
"""

import json
import logging
import os
import re
import time

import requests
from core.memory_schema import build_metadata, validate_source

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8420")
HITL_TTL = 180

_VALID_ENTITY_TYPES = {"Person", "Project", "System", "Concept", "Preference"}
_VALID_RELATIONS = {"KNOWS_ABOUT", "WORKS_ON", "PREFERS", "RELATED_TO", "MANAGES"}
_RELATION_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _hitl_gate(summary: str) -> bool:
    """Request HITL approval showing the exact graph operation to be written."""
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


def _get_conn():
    """Lazy import to get the Lucent SQLite connection."""
    from nervous_system.lucent_api.lucent import _get_connection
    return _get_connection()


def graph_upsert_direct(
    *,
    entity_type: str,
    name: str,
    data_class: str,
    properties: str = "{}",
    relation: str = "",
    target_name: str = "",
    target_type: str = "",
    agent_id: str,
    as_of: str | None = None,
    source: str = "user",
) -> str:
    """Write to the knowledge graph without HITL. Called by the epilogue after batch approval."""
    try:
        try:
            validate_source(source)
        except ValueError as e:
            return json.dumps({"error": str(e)})

        try:
            meta = build_metadata(data_class=data_class, source=source, as_of=as_of)
        except ValueError as e:
            return json.dumps({"error": str(e)})

        label = _validate_label(entity_type)
        props = json.loads(properties) if properties.strip() != "{}" else {}
        props.update(meta)
        props["created_at"] = time.time()

        conn = _get_conn()

        # Extract first_name / last_name from properties if provided
        first_name = props.pop("first_name", None)
        last_name = props.pop("last_name", None)

        # Upsert the node
        cursor = conn.execute(
            """
            INSERT INTO nodes (agent_id, type, name, first_name, last_name,
                               properties, data_class, tier, source, as_of,
                               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_id, name) DO UPDATE SET
                type = excluded.type,
                properties = excluded.properties,
                data_class = excluded.data_class,
                tier = excluded.tier,
                source = excluded.source,
                as_of = excluded.as_of,
                updated_at = excluded.updated_at,
                first_name = COALESCE(excluded.first_name, nodes.first_name),
                last_name = COALESCE(excluded.last_name, nodes.last_name)
            """,
            (
                agent_id, label, name, first_name, last_name,
                json.dumps(props), meta.get("data_class"), meta.get("tier"),
                meta.get("source", source), meta.get("as_of"),
                props["created_at"], props["created_at"],
            ),
        )
        conn.commit()

        # Get the node id
        row = conn.execute(
            "SELECT id FROM nodes WHERE agent_id = ? AND name = ?",
            (agent_id, name),
        ).fetchone()
        node_id = row["id"] if row else cursor.lastrowid

        rel_created = False
        if relation and target_name:
            rel_type = _validate_relation(relation)
            tgt_label = _validate_label(target_type or entity_type)

            # Upsert target node
            conn.execute(
                """
                INSERT INTO nodes (agent_id, type, name, properties, data_class, tier,
                                   source, as_of, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id, name) DO UPDATE SET
                    data_class = excluded.data_class,
                    tier = excluded.tier,
                    source = excluded.source,
                    as_of = excluded.as_of,
                    updated_at = excluded.updated_at
                """,
                (
                    agent_id, tgt_label, target_name, json.dumps(meta),
                    meta.get("data_class"), meta.get("tier"),
                    meta.get("source", source), meta.get("as_of"),
                    props["created_at"], props["created_at"],
                ),
            )
            conn.commit()

            target_row = conn.execute(
                "SELECT id FROM nodes WHERE agent_id = ? AND name = ?",
                (agent_id, target_name),
            ).fetchone()
            target_id = target_row["id"]

            # Upsert edge
            conn.execute(
                """
                INSERT INTO edges (agent_id, source_id, target_id, type,
                                   as_of, source, data_class, tier, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, target_id, type) DO UPDATE SET
                    as_of = excluded.as_of,
                    source = excluded.source,
                    data_class = excluded.data_class,
                    tier = excluded.tier
                """,
                (
                    agent_id, node_id, target_id, rel_type,
                    meta.get("as_of"), meta.get("source", source),
                    meta.get("data_class"), meta.get("tier"),
                    props["created_at"],
                ),
            )
            conn.commit()
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


def graph_upsert(
    *,
    entity_type: str,
    name: str,
    data_class: str,
    properties: str = "{}",
    relation: str = "",
    target_name: str = "",
    target_type: str = "",
    agent_id: str,
    as_of: str | None = None,
    source: str = "user",
) -> str:
    """Add or update a knowledge graph node, optionally linking it to another node.

    Args:
        entity_type: Node label -- one of Person, Project, System, Concept, Preference.
        name: Unique name for this entity (e.g. "Daniel", "Hive Mind").
        properties: JSON string of extra properties to set (e.g. '{"role": "owner"}').
        relation: Relationship type to create (e.g. MANAGES, WORKS_ON). Leave empty for node-only.
        target_name: Name of the target node to link to. Required if relation is set.
        target_type: Entity type of the target node (defaults to entity_type if omitted).
        agent_id: Which agent's graph this belongs to. Required.
        data_class: Data class for this entry (e.g. "person", "preference", "technical-config").
        as_of: ISO datetime for when the fact was established (defaults to now).
        source: Origin of the entry -- "user", "tool", "session", or "self".

    Returns:
        JSON confirmation with node id and relationship created (if any).
    """
    try:
        from nervous_system.lucent_api.kg_guards import check_disambiguation, check_orphan_guard, send_disambiguation_message

        label = _validate_label(entity_type)
        props = json.loads(properties) if properties.strip() != "{}" else {}

        allowed, orphan_msg = check_orphan_guard(relation, target_name)
        if not allowed:
            return json.dumps({"upserted": False, "reason": orphan_msg})

        disambig = check_disambiguation(name, entity_type, agent_id)
        if disambig.action == "disambiguate":
            send_disambiguation_message(name, disambig.existing_nodes)
            return json.dumps({
                "upserted": False,
                "reason": "disambiguation_required",
                "similar_nodes": disambig.existing_nodes,
            })

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


def graph_query(
    entity_name: str,
    agent_id: str,
    depth: int = 1,
) -> str:
    """Retrieve knowledge graph node(s) and their connected relationships.

    Identity-only matching: name (exact, case-insensitive), first_name,
    last_name, or alias-element (substring within the JSON aliases list,
    bounded by JSON quotes for exact-element semantics).

    Property text mentions do NOT match. For mention search, use graph_search.

    Args:
        entity_name: Name to look up. Empty string returns no results.
        agent_id: Which agent's graph to search.
        depth: How many hops to traverse (default 1, max 3).

    Returns:
        JSON with matching nodes, their properties, and connected relationships.
    """
    depth = min(max(depth, 1), 3)
    if not entity_name:
        return json.dumps({"found": False, "entity": entity_name})
    try:
        conn = _get_conn()

        # Identity-only match: name / first_name / last_name (exact, case-insensitive),
        # or alias-element within the JSON aliases array.
        # The alias LIKE pattern is bounded with JSON quotes ('%"<name>"%') so that
        # 'Dan' does not match 'Daniel' unless 'Dan' is itself an array element.
        # json_extract scopes the substring scan to the aliases field only — other
        # property values do not pollute identity lookup.
        alias_pattern = f'%"{entity_name}"%'
        rows = conn.execute(
            """
            SELECT id, name, type, first_name, last_name, properties,
                   data_class, tier, source, as_of, created_at, updated_at
            FROM nodes
            WHERE agent_id = ?
              AND (name = ? COLLATE NOCASE
                   OR first_name = ? COLLATE NOCASE
                   OR last_name = ? COLLATE NOCASE
                   OR json_extract(properties, '$.aliases') LIKE ?)
            """,
            (agent_id, entity_name, entity_name, entity_name, alias_pattern),
        ).fetchall()

        if not rows:
            return json.dumps({"found": False, "entity": entity_name})

        nodes: dict[str, dict] = {}
        for row in rows:
            node_name = row["name"]
            props = json.loads(row["properties"]) if row["properties"] else {}
            props["name"] = node_name
            props["type"] = row["type"]
            props["agent_id"] = agent_id
            if row["first_name"]:
                props["first_name"] = row["first_name"]
            if row["last_name"]:
                props["last_name"] = row["last_name"]
            if row["data_class"]:
                props["data_class"] = row["data_class"]
            if row["tier"]:
                props["tier"] = row["tier"]

            nodes[node_name] = {"properties": props, "connections": []}

            # BFS for connections up to depth
            node_id = row["id"]
            visited = {node_id}
            frontier = [node_id]
            for _d in range(depth):
                next_frontier = []
                for nid in frontier:
                    edges = conn.execute(
                        """
                        SELECT e.type AS rel_type, e.target_id, e.source_id,
                               n.name AS connected_name, n.type AS connected_type,
                               n.properties AS connected_props,
                               n.first_name, n.last_name
                        FROM edges e
                        JOIN nodes n ON (
                            (e.target_id = n.id AND e.source_id = ?)
                            OR (e.source_id = n.id AND e.target_id = ?)
                        )
                        WHERE e.source_id = ? OR e.target_id = ?
                        """,
                        (nid, nid, nid, nid),
                    ).fetchall()

                    for edge in edges:
                        connected_id = (
                            edge["target_id"] if edge["source_id"] == nid
                            else edge["source_id"]
                        )
                        if connected_id in visited:
                            continue
                        visited.add(connected_id)
                        next_frontier.append(connected_id)

                        conn_props = json.loads(edge["connected_props"]) if edge["connected_props"] else {}
                        conn_props["name"] = edge["connected_name"]
                        conn_props["type"] = edge["connected_type"]
                        if edge["first_name"]:
                            conn_props["first_name"] = edge["first_name"]
                        if edge["last_name"]:
                            conn_props["last_name"] = edge["last_name"]

                        direction = "out" if edge["source_id"] == nid else "in"
                        nodes[node_name]["connections"].append({
                            "node": conn_props,
                            "via": [{"type": edge["rel_type"], "direction": direction}],
                        })
                frontier = next_frontier

        matches = list(nodes.values())
        return json.dumps({
            "found": True,
            "count": len(matches),
            "matches": matches,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def graph_search(text: str, limit: int = 25) -> str:
    """Mention search: full-text scan across all property strings.

    Returns nodes whose property values *mention* the query text. This is
    a separate concern from identity lookup (graph_query) — the return
    shape makes that explicit.

    Args:
        text: Substring to search for. Empty string returns an empty list.
        limit: Max number of results (default 25).

    Returns:
        JSON-encoded list of {"node_id", "node_type", "property", "snippet"}.
        One result per matching node — the first property hit wins.
    """
    if not text:
        return json.dumps([])
    try:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT id, type, properties FROM nodes WHERE properties LIKE ? LIMIT ?",
            (f"%{text}%", limit),
        ).fetchall()

        results: list[dict[str, object]] = []
        lower = text.lower()
        for row in rows:
            try:
                props = json.loads(row["properties"]) if row["properties"] else {}
            except json.JSONDecodeError:
                continue
            for k, v in props.items():
                if isinstance(v, str) and lower in v.lower():
                    results.append({
                        "node_id": row["id"],
                        "node_type": row["type"],
                        "property": k,
                        "snippet": _snippet(v, text),
                    })
                    break  # one hit per node — first property wins
        return json.dumps(results)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _snippet(value: str, query: str, ctx_chars: int = 40) -> str:
    """Return a short context window around the first occurrence of query in value."""
    idx = value.lower().find(query.lower())
    if idx == -1:
        return value[: ctx_chars * 2]
    start = max(0, idx - ctx_chars)
    end = min(len(value), idx + len(query) + ctx_chars)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(value) else ""
    return f"{prefix}{value[start:end]}{suffix}"


def search_person(
    first_name: str = "",
    last_name: str = "",
    title: str = "",
    relationship: str = "",
    *,
    agent_id: str,
) -> str:
    """Search for Person nodes by any known name fragment or relationship.

    All parameters are optional but at least one must be provided.
    Matching is case-insensitive substring.
    Multiple params are combined with AND.

    Args:
        first_name: Given name fragment.
        last_name: Surname fragment.
        title: Title or honorific fragment.
        relationship: How this person relates to Daniel.
        agent_id: Which agent's graph to search.

    Returns:
        JSON with matching Person nodes and their properties.
    """
    if not any([first_name, last_name, title, relationship]):
        return json.dumps({"error": "At least one search parameter must be provided."})

    try:
        conn = _get_conn()

        # Build dynamic WHERE clause
        conditions = ["type = 'Person'", "agent_id = ?"]
        params: list[str] = [agent_id]

        if first_name:
            conditions.append("LOWER(COALESCE(first_name, '')) LIKE LOWER(?)")
            params.append(f"%{first_name}%")
        if last_name:
            conditions.append("LOWER(COALESCE(last_name, '')) LIKE LOWER(?)")
            params.append(f"%{last_name}%")
        if title:
            conditions.append("LOWER(COALESCE(json_extract(properties, '$.title'), '')) LIKE LOWER(?)")
            params.append(f"%{title}%")
        if relationship:
            # relationship is stored as a JSON array in properties
            conditions.append("LOWER(COALESCE(json_extract(properties, '$.relationship'), '')) LIKE LOWER(?)")
            params.append(f"%{relationship}%")

        where_clause = " AND ".join(conditions)
        query = f"SELECT * FROM nodes WHERE {where_clause}"

        rows = conn.execute(query, params).fetchall()

        if not rows:
            return json.dumps({"found": False, "query": {
                "first_name": first_name,
                "last_name": last_name,
                "title": title,
                "relationship": relationship,
            }})

        matches = []
        for row in rows:
            props = json.loads(row["properties"]) if row["properties"] else {}
            props["name"] = row["name"]
            props["agent_id"] = row["agent_id"]
            if row["first_name"]:
                props["first_name"] = row["first_name"]
            if row["last_name"]:
                props["last_name"] = row["last_name"]
            matches.append(props)

        return json.dumps({
            "found": True,
            "count": len(matches),
            "matches": matches,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def audit_person_nodes(*, agent_id: str) -> str:
    """Find all Person nodes missing first_name or last_name properties.

    Args:
        agent_id: Which agent's graph to search.

    Returns:
        JSON with found, count, and nodes list.
    """
    try:
        conn = _get_conn()
        rows = conn.execute(
            """
            SELECT id, name, first_name, last_name, properties
            FROM nodes
            WHERE type = 'Person' AND agent_id = ?
              AND (first_name IS NULL OR last_name IS NULL)
            """,
            (agent_id,),
        ).fetchall()

        if not rows:
            return json.dumps({"found": False, "count": 0, "nodes": []})

        nodes_list = []
        for row in rows:
            props = json.loads(row["properties"]) if row["properties"] else {}
            props["name"] = row["name"]
            nodes_list.append({
                "name": row["name"],
                "first_name": row["first_name"],
                "last_name": row["last_name"],
                "element_id": row["id"],
                "properties": props,
            })

        return json.dumps({"found": True, "count": len(nodes_list), "nodes": nodes_list})
    except Exception as e:
        return json.dumps({"error": str(e)})


def update_person_names(
    *,
    name: str,
    first_name: str = "",
    last_name: str = "",
    agent_id: str,
) -> str:
    """Update first_name and/or last_name on a Person node identified by name.

    Args:
        name: The existing name property of the Person node to update.
        first_name: Given name to set (omit or empty string to skip).
        last_name: Surname to set (omit or empty string to skip).
        agent_id: Which agent's graph this belongs to.

    Returns:
        JSON confirmation with updated status.
    """
    if not first_name and not last_name:
        return json.dumps({
            "error": "At least one of first_name or last_name must be provided."
        })

    try:
        conn = _get_conn()

        set_parts = []
        params: list[str] = []

        if first_name:
            set_parts.append("first_name = ?")
            params.append(first_name)
        if last_name:
            set_parts.append("last_name = ?")
            params.append(last_name)

        set_clause = ", ".join(set_parts)
        params.extend([name, agent_id])

        cursor = conn.execute(
            f"""
            UPDATE nodes SET {set_clause}
            WHERE type = 'Person' AND name = ? AND agent_id = ?
            """,
            params,
        )
        conn.commit()

        if cursor.rowcount == 0:
            return json.dumps({
                "updated": False,
                "reason": f"Person node not found with name={name!r} and agent_id={agent_id!r}",
            })

        response: dict[str, object] = {"updated": True, "name": name}
        if first_name:
            response["first_name"] = first_name
        if last_name:
            response["last_name"] = last_name

        return json.dumps(response)
    except Exception as e:
        return json.dumps({"error": str(e)})


# All knowledge graph tool functions for registration
KG_TOOLS = [
    graph_upsert,
    graph_upsert_direct,
    graph_query,
    search_person,
    audit_person_nodes,
    update_person_names,
]
