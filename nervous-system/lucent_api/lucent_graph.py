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
import sqlite3
import time
from datetime import datetime, timezone

import requests
from lucent_api.memory_schema import validate_source
from lucent_api.soul import soul_key_refusal
from lucent_api.schema_registry import get_type_spec

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8420")
HITL_TTL = 180

# Allow-list lives in the schema_types DB table (single runtime source of
# truth). Register new types by POSTing /graph/schema/types — the validator
# picks them up immediately without a restart.
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
    from lucent_api.lucent import _get_connection
    from lucent_api.schema_registry import _all_type_names

    conn = _get_connection()
    if get_type_spec(conn, entity_type) is None:
        raise ValueError(
            f"Invalid entity_type {entity_type!r}. Must be one of: {_all_type_names(conn)}"
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
    from lucent_api.lucent import _get_connection
    return _get_connection()


def _check_identity_guard(conn, name: str, mind_id: str) -> str | None:
    """Return an error string if the caller may not write to this node.

    A node is an identity node when its ``type`` is ``Mind``. Only the
    mind whose ``mind_id`` matches the existing node's ``mind_id``
    (the original creator) may update it. Returns None when the write
    is allowed: either the named node is not a Mind, or the caller is
    the owning mind, or no Mind node by this name exists yet (first
    writer claims ownership).
    """
    row = conn.execute(
        "SELECT mind_id FROM nodes WHERE LOWER(name) = LOWER(?) AND type = 'Mind' LIMIT 1",
        (name,),
    ).fetchone()
    if row is None:
        return None  # not a Mind node, no guard
    if row["mind_id"] == mind_id:
        return None  # caller is the owning mind
    return f"mind_id {mind_id!r} cannot edit identity node for {name!r}"


def graph_upsert_direct(
    *,
    entity_type: str,
    name: str,
    data_class: str,
    properties: str = "{}",
    relation: str = "",
    target_name: str = "",
    target_type: str = "",
    edge_attrs: str = "{}",
    mind_id: str,
    as_of: str | None = None,
    source: str = "user",
) -> str:
    """Write to the knowledge graph without HITL."""
    try:
        try:
            validate_source(source)
        except ValueError as e:
            return json.dumps({"error": str(e)})

        # KG nodes are not vector memories; data_class here is provenance only,
        # not validated against the four-class memory registry. The KG's own
        # entity types (Person, Agent, Memory) live in the `type` column.
        meta = {
            "data_class": data_class or "",
            "tier": "contextual",
            "as_of": as_of or datetime.now(timezone.utc).isoformat(),
            "source": source,
            "superseded": False,
        }

        label = _validate_label(entity_type)
        props = json.loads(properties) if properties.strip() != "{}" else {}
        refusal = soul_key_refusal(props)
        if refusal:
            return refusal
        props.update(meta)
        props["created_at"] = time.time()

        conn = _get_conn()

        # REQ-008: identity-node guard. A mind can only write its own identity node.
        guard_err = _check_identity_guard(conn, name, mind_id)
        if guard_err is not None:
            return json.dumps({"upserted": False, "error": guard_err})

        # Extract first_name / last_name from properties if provided
        first_name = props.pop("first_name", None)
        last_name = props.pop("last_name", None)

        # No DB-level uniqueness on name/type — disambiguation is a
        # pre-write check (kg_guards.check_disambiguation). Find first,
        # then UPDATE existing or INSERT new.
        existing = conn.execute(
            "SELECT id FROM nodes WHERE name = ? AND type = ? LIMIT 1",
            (name, label),
        ).fetchone()
        if existing:
            node_id = existing["id"]
            conn.execute(
                """
                UPDATE nodes SET
                    properties = ?,
                    data_class = ?,
                    tier = ?,
                    source = ?,
                    as_of = ?,
                    updated_at = ?,
                    first_name = COALESCE(?, first_name),
                    last_name = COALESCE(?, last_name)
                WHERE id = ?
                """,
                (
                    json.dumps(props), meta.get("data_class"), meta.get("tier"),
                    meta.get("source", source), meta.get("as_of"),
                    props["created_at"], first_name, last_name, node_id,
                ),
            )
        else:
            cursor = conn.execute(
                """
                INSERT INTO nodes (mind_id, type, name, first_name, last_name,
                                   properties, data_class, tier, source, as_of,
                                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mind_id, label, name, first_name, last_name,
                    json.dumps(props), meta.get("data_class"), meta.get("tier"),
                    meta.get("source", source), meta.get("as_of"),
                    props["created_at"], props["created_at"],
                ),
            )
            node_id = cursor.lastrowid
        conn.commit()

        rel_created = False
        if relation and target_name:
            rel_type = _validate_relation(relation)
            tgt_label = _validate_label(target_type or entity_type)

            # Find-or-create the target node
            existing_target = conn.execute(
                "SELECT id FROM nodes WHERE name = ? AND type = ? LIMIT 1",
                (target_name, tgt_label),
            ).fetchone()
            if existing_target:
                target_id = existing_target["id"]
                conn.execute(
                    """
                    UPDATE nodes SET
                        data_class = ?,
                        tier = ?,
                        source = ?,
                        as_of = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        meta.get("data_class"), meta.get("tier"),
                        meta.get("source", source), meta.get("as_of"),
                        props["created_at"], target_id,
                    ),
                )
            else:
                tgt_cursor = conn.execute(
                    """
                    INSERT INTO nodes (mind_id, type, name, properties, data_class, tier,
                                       source, as_of, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mind_id, tgt_label, target_name, json.dumps(meta),
                        meta.get("data_class"), meta.get("tier"),
                        meta.get("source", source), meta.get("as_of"),
                        props["created_at"], props["created_at"],
                    ),
                )
                target_id = tgt_cursor.lastrowid
            conn.commit()

            # Upsert edge
            edge_props_json = edge_attrs if edge_attrs.strip() else "{}"
            conn.execute(
                """
                INSERT INTO edges (mind_id, source_id, target_id, type,
                                   as_of, source, data_class, tier, created_at, properties)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, target_id, type) DO UPDATE SET
                    as_of = excluded.as_of,
                    source = excluded.source,
                    data_class = excluded.data_class,
                    tier = excluded.tier,
                    properties = excluded.properties
                """,
                (
                    mind_id, node_id, target_id, rel_type,
                    meta.get("as_of"), meta.get("source", source),
                    meta.get("data_class"), meta.get("tier"),
                    props["created_at"], edge_props_json,
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
    mind_id: str,
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
        mind_id: Which agent's graph this belongs to. Required.
        data_class: Data class for this entry (e.g. "person", "preference", "technical-config").
        as_of: ISO datetime for when the fact was established (defaults to now).
        source: Origin of the entry -- "user", "tool", "session", or "self".

    Returns:
        JSON confirmation with node id and relationship created (if any).
    """
    try:
        from lucent_api.kg_guards import check_disambiguation, check_orphan_guard, send_disambiguation_message

        label = _validate_label(entity_type)
        props = json.loads(properties) if properties.strip() != "{}" else {}
        refusal = soul_key_refusal(props)
        if refusal:
            return refusal

        allowed, orphan_msg = check_orphan_guard(relation, target_name)
        if not allowed:
            return json.dumps({"upserted": False, "reason": orphan_msg})

        disambig = check_disambiguation(name, entity_type, mind_id)
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
            mind_id=mind_id,
            data_class=data_class,
            as_of=as_of,
            source=source,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})


def graph_query(
    entity_name: str,
    mind_id: str = "",
    depth: int = 1,
) -> str:
    """Retrieve knowledge graph node(s) and their connected relationships.

    Identity-only matching: name (exact, case-insensitive), first_name,
    last_name, or alias-element (substring within the JSON aliases list,
    bounded by JSON quotes for exact-element semantics).

    Property text mentions do NOT match. For mention search, use graph_search.

    Args:
        entity_name: Name to look up. Empty string returns no results.
        mind_id: Accepted for backward compatibility but ignored. Reads are
            not partitioned by mind — every mind sees every node. mind_id
            is a write-side provenance/protection field only.
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
        # or alias-element within the JSON aliases array. No mind_id filter —
        # nodes are global, mind_id is provenance, not partition.
        alias_pattern = f'%"{entity_name}"%'
        rows = conn.execute(
            """
            SELECT id, name, type, first_name, last_name, properties, mind_id,
                   data_class, tier, source, as_of, created_at, updated_at
            FROM nodes
            WHERE name = ? COLLATE NOCASE
               OR first_name = ? COLLATE NOCASE
               OR last_name = ? COLLATE NOCASE
               OR json_extract(properties, '$.aliases') LIKE ?
            """,
            (entity_name, entity_name, entity_name, alias_pattern),
        ).fetchall()

        if not rows:
            return json.dumps({"found": False, "entity": entity_name})

        nodes: dict[str, dict] = {}
        for row in rows:
            node_name = row["name"]
            props = json.loads(row["properties"]) if row["properties"] else {}
            props["name"] = node_name
            props["type"] = row["type"]
            # mind_id is writer-provenance, not a node attribute.
            # Surface it only on Mind nodes (where it identifies the mind
            # itself); on other node types it's noise.
            if row["type"] == "Mind":
                props["mind_id"] = row["mind_id"]
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
                               e.properties AS edge_props,
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

                        # Surface every edge — `visited` only gates BFS frontier
                        # expansion (cycle prevention), not edge surfacing. Two
                        # nodes may have multiple edges between them (e.g. both
                        # PARENT_OF and CHILD_OF, or FRIEND_OF and COWORKER_OF).
                        conn_props = json.loads(edge["connected_props"]) if edge["connected_props"] else {}
                        conn_props["name"] = edge["connected_name"]
                        conn_props["type"] = edge["connected_type"]
                        if edge["first_name"]:
                            conn_props["first_name"] = edge["first_name"]
                        if edge["last_name"]:
                            conn_props["last_name"] = edge["last_name"]

                        direction = "out" if edge["source_id"] == nid else "in"
                        edge_attrs = json.loads(edge["edge_props"]) if edge["edge_props"] else {}
                        via_entry = {"type": edge["rel_type"], "direction": direction}
                        if edge_attrs:
                            via_entry["attrs"] = edge_attrs
                        nodes[node_name]["connections"].append({
                            "node": conn_props,
                            "via": [via_entry],
                        })

                        if connected_id not in visited:
                            visited.add(connected_id)
                            next_frontier.append(connected_id)
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
    mind_id: str = "",
) -> str:
    """Search for Person nodes by any known name fragment or relationship.

    At least one of first_name/last_name/title/relationship must be provided.
    Matching is case-insensitive substring. Multiple params are combined with AND.

    Args:
        first_name: Given name fragment.
        last_name: Surname fragment.
        title: Title or honorific fragment.
        relationship: How this person relates to Daniel.
        mind_id: Accepted for backward compatibility but ignored. Reads are
            not partitioned by mind — every mind sees every Person node.

    Returns:
        JSON with matching Person nodes and their properties.
    """
    if not any([first_name, last_name, title, relationship]):
        return json.dumps({"error": "At least one search parameter must be provided."})

    try:
        conn = _get_conn()

        # Build dynamic WHERE clause. No mind_id partitioning on reads.
        conditions = ["type = 'Person'"]
        params: list[str] = []

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
            # mind_id is writer-provenance — surface only on Mind nodes.
            # search_person is Person-only, so this never returns mind_id.
            if row["type"] == "Mind":
                props["mind_id"] = row["mind_id"]
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


def audit_person_nodes(*, mind_id: str) -> str:
    """Find all Person nodes missing first_name or last_name properties.

    Args:
        mind_id: Which agent's graph to search.

    Returns:
        JSON with found, count, and nodes list.
    """
    try:
        conn = _get_conn()
        rows = conn.execute(
            """
            SELECT id, name, first_name, last_name, properties
            FROM nodes
            WHERE type = 'Person' AND mind_id = ?
              AND (first_name IS NULL OR last_name IS NULL)
            """,
            (mind_id,),
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
    mind_id: str,
) -> str:
    """Update first_name and/or last_name on a Person node identified by name.

    Args:
        name: The existing name property of the Person node to update.
        first_name: Given name to set (omit or empty string to skip).
        last_name: Surname to set (omit or empty string to skip).
        mind_id: Which agent's graph this belongs to.

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
        params.extend([name, mind_id])

        cursor = conn.execute(
            f"""
            UPDATE nodes SET {set_clause}
            WHERE type = 'Person' AND name = ? AND mind_id = ?
            """,
            params,
        )
        conn.commit()

        if cursor.rowcount == 0:
            return json.dumps({
                "updated": False,
                "reason": f"Person node not found with name={name!r} and mind_id={mind_id!r}",
            })

        response: dict[str, object] = {"updated": True, "name": name}
        if first_name:
            response["first_name"] = first_name
        if last_name:
            response["last_name"] = last_name

        return json.dumps(response)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ---- Property-safe v2 helpers ----
#
# The legacy upsert path is full-replace on the properties blob: passing
# a partial properties dict silently drops every key not in the request,
# and creating an edge via the same call also clobbers the source node's
# properties. These helpers separate the four real intents (create node,
# merge properties, remove properties, manage edges) so callers can't
# accidentally erase a node's body just to add a single field.

# Columns that live outside the properties JSON blob — handled by hand
# when merging/removing.
_COLUMN_KEYS = {"first_name", "last_name"}

# Vector-store columns that leaked onto the nodes table. They have their
# own SQL columns (not properties JSON), and the KG tidy walk needs to
# null them out. NOT in `_PROTECTED_METADATA_KEYS` for this reason.
_VECTOR_LEAKAGE_COLUMNS = {"data_class", "tier", "source"}
# Standard metadata that callers cannot remove via /properties/remove —
# stripping any of these would break the node row's identity, timestamps,
# or staleness tracking. Vector-store fields (`data_class`, `tier`,
# `source`) are intentionally NOT here: they leak onto KG nodes from the
# shared write pipeline and need to be removable during the KG tidy walk.
_PROTECTED_METADATA_KEYS = {
    "name", "type", "created_at", "updated_at", "as_of", "superseded",
}


def graph_node_create(
    *,
    entity_type: str,
    name: str,
    data_class: str,
    mind_id: str,
    properties: str = "{}",
    as_of: str | None = None,
    source: str = "user",
) -> str:
    """Create a new node. Errors if a node with (name, type) already exists.

    Use this when you want creation semantics — not "create-or-replace".
    For property edits on existing nodes use ``graph_properties_merge``.
    """
    try:
        try:
            validate_source(source)
        except ValueError as e:
            return json.dumps({"error": str(e)})

        label = _validate_label(entity_type)
        props = json.loads(properties) if properties.strip() != "{}" else {}
        refusal = soul_key_refusal(props)
        if refusal:
            return refusal

        conn = _get_conn()
        existing = conn.execute(
            "SELECT id FROM nodes WHERE name = ? AND type = ? LIMIT 1",
            (name, label),
        ).fetchone()
        if existing:
            return json.dumps({
                "ok": False,
                "code": "exists",
                "detail": f"node already exists: name={name!r} type={label!r}",
                "id": existing["id"],
            })

        guard_err = _check_identity_guard(conn, name, mind_id)
        if guard_err is not None:
            return json.dumps({"ok": False, "code": "identity_guard", "detail": guard_err})

        meta = {
            "data_class": data_class or "",
            "tier": "contextual",
            "as_of": as_of or datetime.now(timezone.utc).isoformat(),
            "source": source,
            "superseded": False,
        }
        first_name = props.pop("first_name", None)
        last_name = props.pop("last_name", None)
        props.update(meta)
        props["created_at"] = time.time()

        cursor = conn.execute(
            """
            INSERT INTO nodes (mind_id, type, name, first_name, last_name,
                               properties, data_class, tier, source, as_of,
                               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mind_id, label, name, first_name, last_name,
                json.dumps(props), meta["data_class"], meta["tier"],
                meta["source"], meta["as_of"],
                props["created_at"], props["created_at"],
            ),
        )
        conn.commit()
        return json.dumps({"ok": True, "id": cursor.lastrowid, "name": name, "type": label})
    except Exception as e:
        return json.dumps({"ok": False, "code": "internal_error", "detail": str(e)})


def graph_properties_merge(
    *,
    entity_type: str,
    name: str,
    properties: str,
    as_of: str | None = None,
    source: str | None = None,
) -> str:
    """Merge ``properties`` into an existing node's properties blob.

    Keys present in the request are set (overwriting prior values for
    those keys). Keys NOT in the request are preserved. This is the
    additive counterpart to the full-replace upsert.

    ``first_name`` and ``last_name`` in the request map to their columns
    rather than the properties blob, matching the existing storage shape.
    """
    try:
        label = _validate_label(entity_type)
        new_props = json.loads(properties) if properties.strip() else {}
        refusal = soul_key_refusal(new_props)
        if refusal:
            return refusal
        if not isinstance(new_props, dict):
            return json.dumps({"ok": False, "code": "invalid_properties",
                               "detail": "properties must be a JSON object"})

        conn = _get_conn()
        row = conn.execute(
            "SELECT id, properties FROM nodes WHERE name = ? AND type = ? LIMIT 1",
            (name, label),
        ).fetchone()
        if not row:
            return json.dumps({"ok": False, "code": "not_found",
                               "detail": f"node not found: name={name!r} type={label!r}"})

        # Split off column-backed keys.
        first_name = new_props.pop("first_name", None)
        last_name = new_props.pop("last_name", None)

        existing = json.loads(row["properties"]) if row["properties"] else {}
        existing.update(new_props)
        now_iso = as_of or datetime.now(timezone.utc).isoformat()
        existing["updated_at"] = time.time()
        if source is not None:
            existing["source"] = source

        # Build dynamic SET clause for column updates.
        set_parts = ["properties = ?", "updated_at = ?", "as_of = ?"]
        params: list = [json.dumps(existing), existing["updated_at"], now_iso]
        if first_name is not None:
            set_parts.append("first_name = ?")
            params.append(first_name)
        if last_name is not None:
            set_parts.append("last_name = ?")
            params.append(last_name)
        if source is not None:
            set_parts.append("source = ?")
            params.append(source)
        params.append(row["id"])

        conn.execute(f"UPDATE nodes SET {', '.join(set_parts)} WHERE id = ?", params)
        conn.commit()
        return json.dumps({"ok": True, "id": row["id"], "merged_keys": sorted(new_props.keys())})
    except Exception as e:
        return json.dumps({"ok": False, "code": "internal_error", "detail": str(e)})


def graph_properties_remove(
    *,
    entity_type: str,
    name: str,
    keys: list[str],
) -> str:
    """Remove named keys from an existing node's properties blob.

    Rejects requests to remove protected metadata keys (data_class, tier,
    source, as_of, created_at, updated_at, superseded, name, type).
    ``first_name`` / ``last_name`` in ``keys`` clear the corresponding
    column.
    """
    try:
        label = _validate_label(entity_type)
        if not isinstance(keys, list):
            return json.dumps({"ok": False, "code": "invalid_keys",
                               "detail": "keys must be a JSON array"})

        refusal = soul_key_refusal({k: None for k in keys})
        if refusal:
            return refusal

        protected_violations = [k for k in keys if k in _PROTECTED_METADATA_KEYS]
        if protected_violations:
            return json.dumps({
                "ok": False, "code": "protected_metadata",
                "detail": f"these keys cannot be removed: {protected_violations}",
            })

        conn = _get_conn()
        row = conn.execute(
            "SELECT id, properties FROM nodes WHERE name = ? AND type = ? LIMIT 1",
            (name, label),
        ).fetchone()
        if not row:
            return json.dumps({"ok": False, "code": "not_found",
                               "detail": f"node not found: name={name!r} type={label!r}"})

        existing = json.loads(row["properties"]) if row["properties"] else {}
        removed: list[str] = []
        for k in keys:
            if k in _COLUMN_KEYS:
                continue  # handled by NULL on the column below
            if k in existing:
                existing.pop(k)
                removed.append(k)
            # Vector-leakage keys may also live in their dedicated column —
            # the column NULL update below catches that. Either path counts.
        existing["updated_at"] = time.time()

        set_parts = ["properties = ?", "updated_at = ?"]
        params: list = [json.dumps(existing), existing["updated_at"]]
        if "first_name" in keys:
            set_parts.append("first_name = NULL")
            removed.append("first_name")
        if "last_name" in keys:
            set_parts.append("last_name = NULL")
            removed.append("last_name")
        for col in _VECTOR_LEAKAGE_COLUMNS:
            if col in keys:
                set_parts.append(f"{col} = NULL")
                if col not in removed:
                    removed.append(col)
        params.append(row["id"])

        conn.execute(f"UPDATE nodes SET {', '.join(set_parts)} WHERE id = ?", params)
        conn.commit()
        return json.dumps({"ok": True, "id": row["id"], "removed_keys": removed})
    except Exception as e:
        return json.dumps({"ok": False, "code": "internal_error", "detail": str(e)})


def graph_edge_create(
    *,
    source_name: str,
    source_type: str,
    target_name: str,
    target_type: str,
    relation: str,
    edge_attrs: str = "{}",
    data_class: str,
    mind_id: str,
    as_of: str | None = None,
    source: str = "user",
) -> str:
    """Create an edge between two existing nodes. Neither node's properties
    are touched. Both endpoints must already exist; orphan-create is
    intentionally not supported here — it's a separate concern.
    """
    try:
        try:
            validate_source(source)
        except ValueError as e:
            return json.dumps({"error": str(e)})

        rel_type = _validate_relation(relation)
        src_label = _validate_label(source_type)
        tgt_label = _validate_label(target_type)
        attrs = json.loads(edge_attrs) if edge_attrs.strip() != "{}" else {}

        conn = _get_conn()
        src = conn.execute(
            "SELECT id FROM nodes WHERE name = ? AND type = ? LIMIT 1",
            (source_name, src_label),
        ).fetchone()
        if not src:
            return json.dumps({"ok": False, "code": "source_not_found",
                               "detail": f"{src_label}:{source_name} not found"})
        tgt = conn.execute(
            "SELECT id FROM nodes WHERE name = ? AND type = ? LIMIT 1",
            (target_name, tgt_label),
        ).fetchone()
        if not tgt:
            return json.dumps({"ok": False, "code": "target_not_found",
                               "detail": f"{tgt_label}:{target_name} not found"})

        now = time.time()
        try:
            cursor = conn.execute(
                """
                INSERT INTO edges (mind_id, source_id, target_id, type,
                                   as_of, source, data_class, tier, created_at, properties)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mind_id, src["id"], tgt["id"], rel_type,
                    as_of or datetime.now(timezone.utc).isoformat(),
                    source, data_class or "", "contextual", now,
                    json.dumps(attrs),
                ),
            )
            conn.commit()
            return json.dumps({"ok": True, "id": cursor.lastrowid, "relation": rel_type})
        except sqlite3.IntegrityError as e:
            return json.dumps({"ok": False, "code": "edge_exists",
                               "detail": f"edge already exists: {src_label}:{source_name} -[{rel_type}]-> {tgt_label}:{target_name}",
                               "raw": str(e)})
    except Exception as e:
        return json.dumps({"ok": False, "code": "internal_error", "detail": str(e)})


def graph_node_delete(
    *,
    entity_type: str,
    name: str,
) -> str:
    """Hard-delete the node identified by (name, type) and every edge
    touching it (inbound or outbound). Returns counts. Use for true
    duplicates or write-mistakes; for facts that became stale prefer
    setting ``superseded = true`` via /properties/merge.
    """
    try:
        label = _validate_label(entity_type)
        conn = _get_conn()
        row = conn.execute(
            "SELECT id FROM nodes WHERE name = ? AND type = ? LIMIT 1",
            (name, label),
        ).fetchone()
        if not row:
            return json.dumps({"ok": False, "code": "not_found",
                               "detail": f"node not found: name={name!r} type={label!r}"})
        node_id = row["id"]
        edge_cursor = conn.execute(
            "DELETE FROM edges WHERE source_id = ? OR target_id = ?",
            (node_id, node_id),
        )
        edges_removed = edge_cursor.rowcount
        node_cursor = conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        conn.commit()
        return json.dumps({
            "ok": True,
            "id": node_id,
            "edges_deleted": edges_removed,
            "node_deleted": node_cursor.rowcount,
        })
    except Exception as e:
        return json.dumps({"ok": False, "code": "internal_error", "detail": str(e)})


def graph_edge_delete(
    *,
    source_name: str,
    source_type: str,
    target_name: str,
    target_type: str,
    relation: str,
) -> str:
    """Delete the edge(s) matching (source_name, source_type) -[relation]->
    (target_name, target_type). Returns the count deleted.
    """
    try:
        rel_type = _validate_relation(relation)
        src_label = _validate_label(source_type)
        tgt_label = _validate_label(target_type)
        conn = _get_conn()
        src = conn.execute(
            "SELECT id FROM nodes WHERE name = ? AND type = ? LIMIT 1",
            (source_name, src_label),
        ).fetchone()
        tgt = conn.execute(
            "SELECT id FROM nodes WHERE name = ? AND type = ? LIMIT 1",
            (target_name, tgt_label),
        ).fetchone()
        if not src or not tgt:
            return json.dumps({"ok": False, "code": "endpoint_not_found",
                               "detail": "source or target node missing"})
        cursor = conn.execute(
            "DELETE FROM edges WHERE source_id = ? AND target_id = ? AND type = ?",
            (src["id"], tgt["id"], rel_type),
        )
        conn.commit()
        return json.dumps({"ok": True, "deleted": cursor.rowcount})
    except Exception as e:
        return json.dumps({"ok": False, "code": "internal_error", "detail": str(e)})


# All knowledge graph tool functions for registration
KG_TOOLS = [
    graph_upsert,
    graph_upsert_direct,
    graph_query,
    search_person,
    audit_person_nodes,
    update_person_names,
    graph_node_create,
    graph_properties_merge,
    graph_properties_remove,
    graph_edge_create,
    graph_edge_delete,
]
