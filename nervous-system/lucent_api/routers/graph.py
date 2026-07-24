"""Graph endpoints for the lucent-api nervous-system service.

Wraps tools.stateful.lucent_graph public functions in FastAPI routes.
No auth and no HITL — this service is bound to the internal Docker
network and never exposed to the host. Returns native dicts (not the
JSON-string convention the underlying functions inherited from MCP).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from lucent_api.auth import require_admin_bearer

log = logging.getLogger(__name__)

router = APIRouter(prefix="/graph", tags=["graph"])


def _decode(payload: str) -> Any:
    """Parse the JSON-string return convention of the underlying functions."""
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return {"error": "invalid_json_from_underlying_function", "raw": payload}


# ---- Request schemas ----


class UpsertBody(BaseModel):
    entity_type: str
    name: str
    data_class: str
    mind_id: str
    properties: str = "{}"
    relation: str = ""
    target_name: str = ""
    target_type: str = ""
    edge_attrs: str = "{}"
    as_of: str | None = None
    source: str = "user"


class AuditPersonsBody(BaseModel):
    mind_id: str


class UpdatePersonNamesBody(BaseModel):
    name: str
    mind_id: str
    first_name: str = ""
    last_name: str = ""


# ---- v2 property-safe endpoint bodies ----


class NodeCreateBody(BaseModel):
    entity_type: str
    name: str
    data_class: str
    mind_id: str
    properties: str = "{}"
    as_of: str | None = None
    source: str = "user"


class PropertiesMergeBody(BaseModel):
    entity_type: str
    name: str
    properties: str
    as_of: str | None = None
    source: str | None = None


class PropertiesRemoveBody(BaseModel):
    entity_type: str
    name: str
    keys: list[str]


class NodeDeleteBody(BaseModel):
    entity_type: str
    name: str


class EdgeBody(BaseModel):
    source_name: str
    source_type: str
    target_name: str
    target_type: str
    relation: str
    edge_attrs: str = "{}"
    data_class: str = ""
    mind_id: str = ""
    as_of: str | None = None
    source: str = "user"


# ---- Read endpoints ----


@router.get("/query")
def graph_query(
    entity_name: str = Query(...),
    mind_id: str = Query(""),
    depth: int = Query(1, ge=1, le=3),
) -> Any:
    """Retrieve graph node(s) and connected relationships.

    `mind_id` is accepted for backward compatibility but ignored — reads
    are not partitioned by mind. Every mind sees every node.
    """
    from lucent_api.lucent_graph import graph_query as _graph_query

    return _decode(_graph_query(entity_name=entity_name, mind_id=mind_id, depth=depth))


@router.get("/search")
def graph_search(
    text: str = Query(...),
    limit: int = Query(25, ge=1, le=200),
) -> Any:
    """Mention search across all property strings of every node.

    Distinct from /graph/query — returns mention-shape results, not identity matches.
    """
    from lucent_api.lucent_graph import graph_search as _graph_search

    return _decode(_graph_search(text=text, limit=limit))


@router.get("/person/search")
def search_person(
    mind_id: str = Query(""),
    first_name: str = Query(""),
    last_name: str = Query(""),
    title: str = Query(""),
    relationship: str = Query(""),
) -> Any:
    """Fuzzy person search by name fragments, title, or relationship.

    `mind_id` accepted for backward compatibility but ignored.
    """
    from lucent_api.lucent_graph import search_person as _search_person

    return _decode(
        _search_person(
            first_name=first_name,
            last_name=last_name,
            title=title,
            relationship=relationship,
            mind_id=mind_id,
        )
    )


# ---- Write endpoints (guarded but un-HITL'd) ----


@router.post("/upsert-backup")
def graph_upsert_backup(body: UpsertBody) -> Any:
    """Legacy un-validated upsert, kept as a fallback during the schema-enforcement cutover. Delete once `/graph/upsert` (now schema-validating) is proven stable.

    Add or update a graph node, optionally linking to another node.

    **Full-replace semantics.** The ``properties`` blob you send REPLACES
    the existing properties on the node. Keys not in the request are
    dropped. Creating an edge via ``relation``+``target_name`` ALSO
    rewrites the source node's properties as a side-effect. If you only
    want to edit one property or add an edge without touching the node,
    use the v2 endpoints (``/graph/properties/merge`` and ``/graph/edges``).

    Runs the orphan and disambiguation guards from lucent_api.kg_guards. Skips the
    HITL approval step the MCP version had — the nervous system is auth-less
    and trusts the calling mind. Disambiguation conflicts are still surfaced
    in the response so the caller can decide what to do next.
    """
    from lucent_api.kg_guards import (
        check_disambiguation,
        check_orphan_guard,
        send_disambiguation_message,
    )
    from lucent_api.lucent_graph import graph_upsert_direct

    try:
        allowed, orphan_msg = check_orphan_guard(body.relation, body.target_name)
        if not allowed:
            return {"upserted": False, "reason": orphan_msg}

        disambig = check_disambiguation(body.name, body.entity_type, body.mind_id)
        if disambig.action == "disambiguate":
            send_disambiguation_message(body.name, disambig.existing_nodes)
            return {
                "upserted": False,
                "reason": "disambiguation_required",
                "similar_nodes": disambig.existing_nodes,
            }

        return _decode(
            graph_upsert_direct(
                entity_type=body.entity_type,
                name=body.name,
                properties=body.properties,
                relation=body.relation,
                target_name=body.target_name,
                target_type=body.target_type,
                edge_attrs=body.edge_attrs,
                mind_id=body.mind_id,
                data_class=body.data_class,
                as_of=body.as_of,
                source=body.source,
            )
        )
    except Exception as e:
        log.exception("graph_upsert_backup failed")
        return {"upserted": False, "error": str(e)}


@router.post("/upsert")
def graph_upsert(body: UpsertBody) -> Any:
    """Schema-validated upsert.

    Runs the schema validations from `schema_registry` BEFORE the existing
    disambiguation + orphan guards. On validation failure returns the
    structured error shape from the tidy spec so the calling mind knows
    exactly what was wrong and how to escalate (via
    `POST /schema/request-new-type` in hive-tools).
    """
    import json as _json

    from lucent_api.kg_guards import (
        check_disambiguation,
        check_orphan_guard,
        send_disambiguation_message,
    )
    from lucent_api.lucent_graph import graph_upsert_direct
    from lucent_api.schema_registry import validate_edge, validate_node

    try:
        # 1. Node validation — type allow-list, property schema, enums.
        props = _json.loads(body.properties) if body.properties.strip() != "{}" else {}
        ok, code, detail = validate_node(body.entity_type, props)
        if not ok:
            return {
                "ok": False,
                "code": code,
                **(detail or {}),
                "request_new_via": "POST /schema/request-new-type",
            }

        # 2. Edge validation (if a relation was supplied). The strict
        #    endpoint requires `target_type` when relation is set — the
        #    legacy endpoint inferred it; we require it here so the edge
        #    direction check has both ends.
        if body.relation:
            if not body.target_type:
                return {
                    "ok": False,
                    "code": "missing_target_type",
                    "detail": "target_type is required when relation is set on /graph/upsert",
                }
            edge_attrs = _json.loads(body.edge_attrs) if body.edge_attrs.strip() != "{}" else {}
            ok, code, detail = validate_edge(
                body.relation,
                body.entity_type,
                body.target_type,
                attrs=edge_attrs or None,
            )
            if not ok:
                return {
                    "ok": False,
                    "code": code,
                    **(detail or {}),
                    "request_new_via": "POST /schema/request-new-type",
                }

        # 3. Existing pre-write guards (orphan, disambiguation).
        allowed, orphan_msg = check_orphan_guard(body.relation, body.target_name)
        if not allowed:
            return {"ok": False, "code": "orphan_blocked", "detail": orphan_msg}

        disambig = check_disambiguation(body.name, body.entity_type, body.mind_id)
        if disambig.action == "disambiguate":
            send_disambiguation_message(body.name, disambig.existing_nodes)
            return {
                "ok": False,
                "code": "disambiguation_required",
                "detail": f"existing node(s) match {body.name!r}",
                "similar_nodes": disambig.existing_nodes,
            }

        # 4. Delegate to the existing write path.
        write_result = _decode(
            graph_upsert_direct(
                entity_type=body.entity_type,
                name=body.name,
                properties=body.properties,
                relation=body.relation,
                target_name=body.target_name,
                target_type=body.target_type,
                edge_attrs=body.edge_attrs,
                mind_id=body.mind_id,
                data_class=body.data_class,
                as_of=body.as_of,
                source=body.source,
            )
        )
        # Normalize the success envelope. The underlying function returns
        # its own shape; wrap it consistently.
        if isinstance(write_result, dict) and "error" in write_result:
            return {"ok": False, "code": "write_error", "detail": write_result["error"]}
        return {"ok": True, **(write_result if isinstance(write_result, dict) else {})}
    except Exception as e:
        log.exception("graph_upsert failed")
        return {"ok": False, "code": "internal_error", "detail": str(e)}


@router.get("/schema")
def get_schema() -> dict:
    """Return the full schema allow-list.

    Read by callers (typically minds) before they construct a write so they
    can choose a valid type/edge and avoid round-trips on rejection.
    """
    from lucent_api.lucent import _get_connection
    from lucent_api.schema_registry import all_edges_from_db, all_types_from_db

    conn = _get_connection()
    return {
        "types": all_types_from_db(conn),
        "edges": all_edges_from_db(conn),
    }


# ---- Admin: schema mutation (bearer-gated, hive-tools only) ----


class SchemaTypeBody(BaseModel):
    name: str
    kind: str  # "first-class" | "second-class"
    property_schema: dict


class SchemaEdgeBody(BaseModel):
    name: str
    source_type: str
    target_type: str
    attr_schema: dict
    symmetric: bool = False


@router.post("/schema/types", dependencies=[Depends(require_admin_bearer)])
def add_schema_type(body: SchemaTypeBody) -> dict:
    """Insert a new node type into the allow-list.

    Bearer-gated by ``LUCENT_ADMIN_BEARER_TOKEN``. Only hive-tools (which
    fronts the request-new-type HITL flow) holds this token.
    """
    import json as _json
    import time

    from lucent_api.lucent import _get_connection

    if body.kind not in ("first-class", "second-class"):
        return {"ok": False, "code": "invalid_kind", "detail": "kind must be first-class or second-class"}

    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO schema_types (name, kind, property_schema, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (body.name, body.kind, _json.dumps(body.property_schema), time.time()),
        )
        conn.commit()
        return {"ok": True, "name": body.name}
    except Exception as e:
        return {"ok": False, "code": "duplicate_or_db_error", "detail": str(e)}


@router.post("/schema/edges", dependencies=[Depends(require_admin_bearer)])
def add_schema_edge(body: SchemaEdgeBody) -> dict:
    """Insert a new edge type into the allow-list. Bearer-gated."""
    import json as _json
    import time

    from lucent_api.lucent import _get_connection

    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO schema_edges
                (name, source_type, target_type, attr_schema, symmetric, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                body.name,
                body.source_type,
                body.target_type,
                _json.dumps(body.attr_schema),
                1 if body.symmetric else 0,
                time.time(),
            ),
        )
        conn.commit()
        return {"ok": True, "name": body.name}
    except Exception as e:
        return {"ok": False, "code": "duplicate_or_db_error", "detail": str(e)}


@router.post("/upsert-direct")
def graph_upsert_direct_endpoint(body: UpsertBody) -> Any:
    """Bypass guards and write directly. Use only when the caller has
    already validated the entity (e.g., during ingestion replays)."""
    from lucent_api.lucent_graph import graph_upsert_direct

    return _decode(
        graph_upsert_direct(
            entity_type=body.entity_type,
            name=body.name,
            properties=body.properties,
            relation=body.relation,
            target_name=body.target_name,
            target_type=body.target_type,
            edge_attrs=body.edge_attrs,
            mind_id=body.mind_id,
            data_class=body.data_class,
            as_of=body.as_of,
            source=body.source,
        )
    )


# ---- v2 property-safe endpoints ----
#
# The legacy /graph/upsert family is full-replace on the properties blob
# and writes properties on the source node as a side-effect of edge
# creation. These endpoints split those concerns so callers can edit a
# node without erasing the rest of it.


@router.post("/nodes")
def graph_nodes_create(body: NodeCreateBody) -> Any:
    """Create a new node. Rejects if a node with (name, type) already exists.

    For property edits on an existing node use /graph/properties/merge.
    """
    from lucent_api.lucent_graph import graph_node_create

    return _decode(
        graph_node_create(
            entity_type=body.entity_type,
            name=body.name,
            data_class=body.data_class,
            mind_id=body.mind_id,
            properties=body.properties,
            as_of=body.as_of,
            source=body.source,
        )
    )


@router.post("/properties/merge")
def graph_properties_merge_endpoint(body: PropertiesMergeBody) -> Any:
    """Merge properties into an existing node. Keys not in the request are
    preserved — the additive counterpart to the full-replace upsert.
    """
    from lucent_api.lucent_graph import graph_properties_merge

    return _decode(
        graph_properties_merge(
            entity_type=body.entity_type,
            name=body.name,
            properties=body.properties,
            as_of=body.as_of,
            source=body.source,
        )
    )


@router.post("/properties/remove")
def graph_properties_remove_endpoint(body: PropertiesRemoveBody) -> Any:
    """Remove named keys from an existing node's properties blob.

    Protected metadata (name, type, created_at, updated_at, as_of,
    superseded) cannot be removed via this path. Vector-store columns
    (`data_class`, `tier`, `source`) ARE removable — they leak onto KG
    nodes from the shared write pipeline and the tidy walk strips them.
    """
    from lucent_api.lucent_graph import graph_properties_remove

    return _decode(
        graph_properties_remove(
            entity_type=body.entity_type,
            name=body.name,
            keys=body.keys,
        )
    )


@router.post("/edges")
def graph_edges_create(body: EdgeBody) -> Any:
    """Create an edge between two existing nodes. Neither endpoint's
    properties are touched. Both nodes must already exist.
    """
    from lucent_api.lucent_graph import graph_edge_create

    return _decode(
        graph_edge_create(
            source_name=body.source_name,
            source_type=body.source_type,
            target_name=body.target_name,
            target_type=body.target_type,
            relation=body.relation,
            edge_attrs=body.edge_attrs,
            data_class=body.data_class,
            mind_id=body.mind_id,
            as_of=body.as_of,
            source=body.source,
        )
    )


@router.delete("/edges")
def graph_edges_delete(body: EdgeBody) -> Any:
    """Delete edge(s) matching the (source, relation, target) tuple."""
    from lucent_api.lucent_graph import graph_edge_delete

    return _decode(
        graph_edge_delete(
            source_name=body.source_name,
            source_type=body.source_type,
            target_name=body.target_name,
            target_type=body.target_type,
            relation=body.relation,
        )
    )


@router.delete("/nodes")
def graph_nodes_delete(body: NodeDeleteBody) -> Any:
    """Hard-delete a node and every edge touching it. Use for true
    duplicates or write-mistakes; for facts that became stale prefer
    setting ``superseded = true`` via /properties/merge.
    """
    from lucent_api.lucent_graph import graph_node_delete

    return _decode(
        graph_node_delete(entity_type=body.entity_type, name=body.name)
    )


# ---- Maintenance endpoints ----


@router.post("/persons/audit")
def audit_persons(body: AuditPersonsBody) -> Any:
    """Audit Person nodes for missing first/last name properties."""
    from lucent_api.lucent_graph import audit_person_nodes

    return _decode(audit_person_nodes(mind_id=body.mind_id))


@router.post("/persons/update-names")
def update_person_names(body: UpdatePersonNamesBody) -> Any:
    """Backfill first_name/last_name on a Person node."""
    from lucent_api.lucent_graph import update_person_names as _update_names

    return _decode(
        _update_names(
            name=body.name,
            first_name=body.first_name,
            last_name=body.last_name,
            mind_id=body.mind_id,
        )
    )


@router.get("/raw-properties")
def graph_raw_properties(
    name: str = Query(...),
    mind_id: str = Query(""),
) -> dict:
    """Return a node's raw properties blob, unflattened.

    Use this when you need to round-trip the JSON blob through `/graph/upsert`
    without losing inner-blob keys that collide with column names (e.g.
    `data_class`, `tier`, `as_of`, `source`, `created_at`, `superseded`,
    `type`). The standard `/graph/query` flattens column values over inner
    blob keys, so collision-prone keys are silently overwritten on reads.

    `mind_id` accepted for backward compatibility but ignored.

    Response shape:
      - `{"found": true, "properties": {...}, "mind_id": "..."}` — node exists.
      - `{"found": false}` — no matching node.
    """
    from lucent_api.lucent import _get_connection

    conn = _get_connection()
    row = conn.execute(
        "SELECT type, properties, mind_id FROM nodes WHERE LOWER(name)=LOWER(?) LIMIT 1",
        (name,),
    ).fetchone()
    if row is None:
        return {"found": False}
    try:
        blob = json.loads(row["properties"]) if row["properties"] else {}
    except (TypeError, ValueError):
        blob = {}
    # mind_id is writer-provenance — surface only on Mind nodes.
    result = {"found": True, "properties": blob}
    if row["type"] == "Mind":
        result["mind_id"] = row["mind_id"]
    return result


@router.get("/data")
def graph_data(limit: int = Query(400, ge=1, le=2000)) -> dict:
    """Visualization export — flat nodes + edges.

    Returns up to `limit` nodes (ordered by id) plus all edges that connect
    pairs within that node set. Used by Spark-to-Bloom's Cytoscape view.
    """
    from lucent_api.lucent import _get_connection

    conn = _get_connection()  # singleton — do not close
    node_rows = conn.execute(
        "SELECT id, name, type, properties FROM nodes ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    nodes = []
    for r in node_rows:
        props = json.loads(r["properties"]) if r["properties"] else {}
        nodes.append(
            {
                "id": r["id"],
                "label": r["name"],
                "type": r["type"],
                **{k: v for k, v in props.items() if k not in ("id", "label", "type")},
            }
        )
    node_ids = {r["id"] for r in node_rows}
    edge_rows = conn.execute(
        "SELECT source_id, target_id, type FROM edges"
    ).fetchall()
    edges = [
        {"source": r["source_id"], "target": r["target_id"], "type": r["type"]}
        for r in edge_rows
        if r["source_id"] in node_ids and r["target_id"] in node_ids
    ]
    return {"nodes": nodes, "edges": edges}
