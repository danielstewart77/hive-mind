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

from fastapi import APIRouter, Query
from pydantic import BaseModel

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
    agent_id: str
    properties: str = "{}"
    relation: str = ""
    target_name: str = ""
    target_type: str = ""
    as_of: str | None = None
    source: str = "user"


class AuditPersonsBody(BaseModel):
    agent_id: str


class UpdatePersonNamesBody(BaseModel):
    name: str
    agent_id: str
    first_name: str = ""
    last_name: str = ""


# ---- Read endpoints ----


@router.get("/query")
def graph_query(
    entity_name: str = Query(...),
    agent_id: str = Query(...),
    depth: int = Query(1, ge=1, le=3),
) -> Any:
    """Retrieve graph node(s) and connected relationships."""
    from nervous_system.lucent_api.lucent_graph import graph_query as _graph_query

    return _decode(_graph_query(entity_name=entity_name, agent_id=agent_id, depth=depth))


@router.get("/search")
def graph_search(
    text: str = Query(...),
    limit: int = Query(25, ge=1, le=200),
) -> Any:
    """Mention search across all property strings of every node.

    Distinct from /graph/query — returns mention-shape results, not identity matches.
    """
    from nervous_system.lucent_api.lucent_graph import graph_search as _graph_search

    return _decode(_graph_search(text=text, limit=limit))


@router.get("/person/search")
def search_person(
    agent_id: str = Query(...),
    first_name: str = Query(""),
    last_name: str = Query(""),
    title: str = Query(""),
    relationship: str = Query(""),
) -> Any:
    """Fuzzy person search by name fragments, title, or relationship."""
    from nervous_system.lucent_api.lucent_graph import search_person as _search_person

    return _decode(
        _search_person(
            first_name=first_name,
            last_name=last_name,
            title=title,
            relationship=relationship,
            agent_id=agent_id,
        )
    )


# ---- Write endpoints (guarded but un-HITL'd) ----


@router.post("/upsert")
def graph_upsert(body: UpsertBody) -> Any:
    """Add or update a graph node, optionally linking to another node.

    Runs the orphan and disambiguation guards from nervous_system.lucent_api.kg_guards. Skips the
    HITL approval step the MCP version had — the nervous system is auth-less
    and trusts the calling mind. Disambiguation conflicts are still surfaced
    in the response so the caller can decide what to do next.
    """
    from nervous_system.lucent_api.kg_guards import (
        check_disambiguation,
        check_orphan_guard,
        send_disambiguation_message,
    )
    from nervous_system.lucent_api.lucent_graph import graph_upsert_direct

    try:
        allowed, orphan_msg = check_orphan_guard(body.relation, body.target_name)
        if not allowed:
            return {"upserted": False, "reason": orphan_msg}

        disambig = check_disambiguation(body.name, body.entity_type, body.agent_id)
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
                agent_id=body.agent_id,
                data_class=body.data_class,
                as_of=body.as_of,
                source=body.source,
            )
        )
    except Exception as e:
        log.exception("graph_upsert failed")
        return {"upserted": False, "error": str(e)}


@router.post("/upsert-direct")
def graph_upsert_direct_endpoint(body: UpsertBody) -> Any:
    """Bypass guards and write directly. Use only when the caller has
    already validated the entity (e.g., during ingestion replays)."""
    from nervous_system.lucent_api.lucent_graph import graph_upsert_direct

    return _decode(
        graph_upsert_direct(
            entity_type=body.entity_type,
            name=body.name,
            properties=body.properties,
            relation=body.relation,
            target_name=body.target_name,
            target_type=body.target_type,
            agent_id=body.agent_id,
            data_class=body.data_class,
            as_of=body.as_of,
            source=body.source,
        )
    )


# ---- Maintenance endpoints ----


@router.post("/persons/audit")
def audit_persons(body: AuditPersonsBody) -> Any:
    """Audit Person nodes for missing first/last name properties."""
    from nervous_system.lucent_api.lucent_graph import audit_person_nodes

    return _decode(audit_person_nodes(agent_id=body.agent_id))


@router.post("/persons/update-names")
def update_person_names(body: UpdatePersonNamesBody) -> Any:
    """Backfill first_name/last_name on a Person node."""
    from nervous_system.lucent_api.lucent_graph import update_person_names as _update_names

    return _decode(
        _update_names(
            name=body.name,
            first_name=body.first_name,
            last_name=body.last_name,
            agent_id=body.agent_id,
        )
    )
