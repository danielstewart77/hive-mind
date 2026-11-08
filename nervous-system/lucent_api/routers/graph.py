"""Graph endpoints for the lucent-api nervous-system service.

Wraps the `lucent_api.lucent_graph` public functions in FastAPI routes.
Every memory and graph route is gated on a per-holder credential; which
mind a request speaks for is read off the value it presented. Returns
native dicts (not the JSON-string convention the underlying functions
inherited from MCP).

Every route is `async def` now, because the store is. A synchronous route
over an async access layer would need a bridge, and the bridge would be the
one place in the hive that blocks an event loop on a database call.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel, Field

import hive_data

from lucent_api.auth import require_admin_bearer
from lucent_api.holder_access import Caller, require_holder
from lucent_api.soul import soul_key_refusal

log = logging.getLogger(__name__)

router = APIRouter(
    dependencies=[Depends(require_holder)],
    prefix="/graph", tags=["graph"])


def _decode(payload: str) -> Any:
    """Parse the JSON-string return convention of the underlying functions.

    A soul-key refusal becomes a real 403 rather than a 200 carrying an
    error body. Every caller of these routes is fire-and-forget shell —
    curl piped into jq, with no status check — and a refusal that returns
    200 is a refusal nobody notices.

    The same reasoning now covers the database's own refusals. A conflict,
    an absent foreign key, a value the column will not take, a statement
    that ran out of time, an exhausted pool and an unreachable server were
    all impossible against a local file and are all routine against a
    socket. Each arrives carrying the status it should be answered with —
    `http_status` in the body — and is raised rather than returned, for
    exactly the reason the soul refusal is.
    """
    from lucent_api.soul import SOUL_KEY_REFUSED

    try:
        body = json.loads(payload)
    except json.JSONDecodeError:
        return {"error": "invalid_json_from_underlying_function", "raw": payload}
    if isinstance(body, dict) and body.get("code") == SOUL_KEY_REFUSED:
        raise HTTPException(403, body.get("detail", "soul writes are not allowed here"))
    if isinstance(body, dict) and body.get("http_status"):
        raise HTTPException(
            int(body["http_status"]),
            str(body.get("detail") or body.get("code") or "refused"))
    return body


#: What a `hive_data` refusal is answered with when it escapes a route
#: rather than one of the wrapped functions — the guards and the registry
#: writes that talk to the store directly.
_STORE_STATUS: tuple[tuple[type[Exception], int], ...] = (
    (hive_data.Conflict, 409),
    (hive_data.MissingReference, 409),
    (hive_data.ValueRefused, 400),
    (hive_data.NoConnectionAvailable, 503),
    (hive_data.TimedOut, 503),
    (hive_data.Unreachable, 503),
    (hive_data.NotInitialised, 503),
    (hive_data.NotConfigured, 503),
    (hive_data.CredentialRefused, 503),
    (hive_data.DatabaseMissing, 503),
    (hive_data.SchemaMismatch, 503),
)


def _store_http(exc: hive_data.HiveDataError) -> HTTPException:
    """One database refusal as the HTTP refusal that names it."""
    for kind, status in _STORE_STATUS:
        if isinstance(exc, kind):
            return HTTPException(status, f"{type(exc).__name__}: {exc}")
    return HTTPException(503, f"{type(exc).__name__}: {exc}")


# ---- Request schemas ----


class UpsertBody(BaseModel):
    entity_type: str
    name: str
    data_class: str
    mind_id: str
    # Whose graph this write lands in. A mind may only name its own owner
    # and need not name one at all; a service or an admin names the owner it
    # is acting for, and writes into its own when it names none.
    owner: str = ""
    properties: str = "{}"
    relation: str = ""
    target_name: str = ""
    target_type: str = ""
    edge_attrs: str = "{}"
    as_of: str | None = None
    source: str = "user"


class AuditPersonsBody(BaseModel):
    mind_id: str = ""
    # Whose graph this write lands in. A mind may only name its own owner
    # and need not name one at all; a service or an admin names the owner it
    # is acting for, and writes into its own when it names none.
    owner: str = ""


class UpdatePersonNamesBody(BaseModel):
    name: str
    mind_id: str = ""
    # Whose graph this write lands in. A mind may only name its own owner
    # and need not name one at all; a service or an admin names the owner it
    # is acting for, and writes into its own when it names none.
    owner: str = ""
    first_name: str = ""
    last_name: str = ""


# ---- v2 property-safe endpoint bodies ----


class NodeCreateBody(BaseModel):
    entity_type: str
    name: str
    data_class: str
    mind_id: str
    # Whose graph this write lands in. A mind may only name its own owner
    # and need not name one at all; a service or an admin names the owner it
    # is acting for, and writes into its own when it names none.
    owner: str = ""
    properties: str = "{}"
    as_of: str | None = None
    source: str = "user"


class PropertiesMergeBody(BaseModel):
    entity_type: str
    name: str
    properties: str
    # Whose graph this write lands in. A mind may only name its own owner
    # and need not name one at all; a service or an admin names the owner it
    # is acting for, and writes into its own when it names none.
    owner: str = ""
    as_of: str | None = None
    source: str | None = None


class PropertiesRemoveBody(BaseModel):
    entity_type: str
    name: str
    keys: list[str]
    # Whose graph this write lands in. A mind may only name its own owner
    # and need not name one at all; a service or an admin names the owner it
    # is acting for, and writes into its own when it names none.
    owner: str = ""


class NodeDeleteBody(BaseModel):
    entity_type: str
    name: str
    # Whose graph this write lands in. A mind may only name its own owner
    # and need not name one at all; a service or an admin names the owner it
    # is acting for, and writes into its own when it names none.
    owner: str = ""


class EdgeBody(BaseModel):
    source_name: str
    source_type: str
    target_name: str
    target_type: str
    relation: str
    edge_attrs: str = "{}"
    data_class: str = ""
    mind_id: str = ""
    # Whose graph this write lands in. A mind may only name its own owner
    # and need not name one at all; a service or an admin names the owner it
    # is acting for, and writes into its own when it names none.
    owner: str = ""
    as_of: str | None = None
    source: str = "user"


# ---- Read endpoints ----


# A prompt's candidate names are a person's name repeated N times over the
# wire. Sent as a query string each one landed verbatim in Zeek's http.log,
# in uvicorn's access log and in Loki, where the sentinel read the hive's own
# people back as network events. They travel in a body now, all of them at
# once — a turn naming a dozen people cost a dozen serial round trips on the
# hot path of every prompt.
#
# The ceiling is on the caller's behalf, not the store's: the retrieval hook
# treats every capitalised word as a candidate, and a pasted file yields
# hundreds. 256 is well above any real sentence and below the point where one
# request is doing a table scan per name for a paragraph nobody named anyone
# in. A name past 200 characters is not a name, and is refused on its own
# entry rather than taking the request down with it.
MAX_QUERY_NAMES = 256
MAX_NAME_CHARS = 200


class GraphQueryBody(BaseModel):
    """Body for ``POST /graph/query``.

    ``mind_id`` is accepted and ignored, as it was on the query string —
    reads are not partitioned by mind. It stays on the schema so a caller
    still sending it gets an answer rather than a 422.
    """

    names: list[str]
    depth: int = Field(1, ge=1, le=3)
    mind_id: str = ""


@router.post("/query")
async def graph_query_batch(
    body: GraphQueryBody, caller: Caller = Depends(require_holder)
) -> Any:
    """Resolve every name in one request, answering each independently.

    One name's failure is its own: a row the store cannot read, or a name
    past the length ceiling, comes back as that entry's ``error`` while its
    siblings answer normally. Folding the batch into a single 5xx — or a
    single 413 — would cost a turn every person it mentioned because one
    token in the prompt was not a name.

    Names repeated within a request are answered once, in first-seen order.
    """
    from lucent_api import lucent_graph

    if len(body.names) > MAX_QUERY_NAMES:
        raise HTTPException(
            413,
            f"{len(body.names)} names exceeds the {MAX_QUERY_NAMES}-name ceiling",
        )

    seen: set[str] = set()
    results: list[dict] = []
    for name in body.names:
        if name in seen:
            continue
        seen.add(name)
        entry: dict[str, Any] = {"entity": name, "found": False, "count": 0, "matches": []}
        if len(name) > MAX_NAME_CHARS:
            # This name's refusal, not the request's. A prompt carrying one
            # long alphabetic run — a pasted sequence, a generated
            # identifier — tokenises into a candidate nobody typed, and a
            # whole-request 413 would cost the turn every real person it
            # mentioned alongside it.
            entry["error"] = f"a name of {len(name)} characters exceeds {MAX_NAME_CHARS}"
            results.append(entry)
            continue
        try:
            answer = _decode(
                await lucent_graph.graph_query(
                    entity_name=name,
                    depth=body.depth,
                    owners=caller.owners_for_read(),
                )
            )
        except HTTPException as refused:
            # A refusal about THIS name is this name's error. `_decode`
            # turns a store refusal carrying `http_status` into an
            # HTTPException, and re-raising it here answered 400 for the
            # whole request over one token the database could not read —
            # the exact cost this route's docstring promises not to pay. On
            # the hot path of every prompt that is a turn with no
            # identities resolved at all.
            #
            # A store that is not answering is the exception: every entry
            # would carry the same error, and two hundred with a list of
            # them is how an outage goes unnoticed for a month. So a 5xx
            # still fails the request.
            if refused.status_code >= 500:
                raise
            entry["error"] = str(refused.detail)
            results.append(entry)
            continue
        except Exception as e:
            entry["error"] = str(e)
            results.append(entry)
            continue
        if isinstance(answer, dict) and answer.get("error"):
            entry["error"] = str(answer["error"])
        elif isinstance(answer, dict) and answer.get("found"):
            entry["found"] = True
            entry["count"] = answer.get("count", 0)
            entry["matches"] = answer.get("matches") or []
        results.append(entry)
    return {"results": results}


@router.get("/search")
async def graph_search(
    text: str = Query(...),
    limit: int = Query(25, ge=1, le=200),
    caller: Caller = Depends(require_holder),
) -> Any:
    """Mention search across all property strings of every node.

    Distinct from /graph/query — returns mention-shape results, not identity matches.
    """
    from lucent_api.lucent_graph import graph_search as _graph_search

    return _decode(
        await _graph_search(
            text=text, limit=limit, owners=caller.owners_for_read())
    )


@router.get("/person/search")
async def search_person(
    mind_id: str = Query(""),
    first_name: str = Query(""),
    last_name: str = Query(""),
    title: str = Query(""),
    relationship: str = Query(""),
    caller: Caller = Depends(require_holder),
) -> Any:
    """Fuzzy person search by name fragments, title, or relationship.

    `mind_id` accepted for backward compatibility but ignored.
    """
    from lucent_api.lucent_graph import search_person as _search_person

    return _decode(
        await _search_person(
            first_name=first_name,
            last_name=last_name,
            title=title,
            relationship=relationship,
            mind_id=mind_id,
            owners=caller.owners_for_read(),
        )
    )


# ---- Write endpoints (guarded but un-HITL'd) ----


@router.post("/upsert-backup")
async def graph_upsert_backup(
    body: UpsertBody, caller: Caller = Depends(require_holder)
) -> Any:
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
    import json as _json

    from lucent_api.kg_guards import (
        check_disambiguation,
        check_orphan_guard,
        send_disambiguation_message,
    )
    from lucent_api.lucent_graph import _call, graph_upsert_direct

    # Ahead of the orphan and disambiguation guards, and outside the try:
    # those answer 200 with a `reason`, so a soul write that trips one is
    # refused for the wrong cause and would start working the moment the
    # caller supplied an edge.
    try:
        _probe = _json.loads(body.properties) if body.properties.strip() != "{}" else {}
    except Exception:
        _probe = {}
    _refusal = soul_key_refusal(_probe)
    if _refusal:
        return _decode(_refusal)

    try:
        allowed, orphan_msg = check_orphan_guard(body.relation, body.target_name)
        if not allowed:
            return {"upserted": False, "reason": orphan_msg}

        disambig = await _call(
            check_disambiguation,
            body.name,
            body.entity_type,
            caller.mind_for_write(body.mind_id, required=False),
            caller.owner_for_write(body.owner),
        )
        if disambig.action == "disambiguate":
            await _call(
                send_disambiguation_message, body.name, disambig.existing_nodes)
            return {
                "upserted": False,
                "reason": "disambiguation_required",
                "similar_nodes": disambig.existing_nodes,
            }

        return _decode(
            await graph_upsert_direct(
                entity_type=body.entity_type,
                name=body.name,
                properties=body.properties,
                relation=body.relation,
                target_name=body.target_name,
                target_type=body.target_type,
                edge_attrs=body.edge_attrs,
                mind_id=caller.mind_for_write(body.mind_id, required=False),
                owner=caller.owner_for_write(body.owner),
                stamp=caller.owner_to_stamp(body.owner),
                data_class=body.data_class,
                as_of=body.as_of,
                source=body.source,
            )
        )
    except HTTPException:
        # A soul refusal is an HTTPException, and HTTPException is an
        # Exception: caught below it would come back as 200 with the 403
        # in a string field, which is the shape `_decode` exists to avoid.
        raise
    except hive_data.HiveDataError as refused:
        # Likewise the database's refusals: reported as a 200 with a string
        # they are a failure the caller is told nothing about.
        log.warning("graph_upsert_backup refused by the store: %s", refused)
        raise _store_http(refused) from refused
    except Exception as e:
        log.exception("graph_upsert_backup failed")
        return {"upserted": False, "error": str(e)}


@router.post("/upsert")
async def graph_upsert(
    body: UpsertBody, caller: Caller = Depends(require_holder)
) -> Any:
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
    from lucent_api.lucent_graph import _call, graph_upsert_direct
    from lucent_api.schema_registry import (
        load_registry, validate_edge, validate_node,
    )

    # The soul boundary, ahead of schema validation and outside the try:
    # the Mind schema happens not to allow `soul_values` today, so a soul
    # write here already fails — but as `unknown_field` with a 200, which
    # reads as a schema gap somebody could reasonably widen. The refusal has
    # to be the one that means what it says, and a 403 raised inside the
    # block below would be caught by its own `except Exception` and turned
    # back into a 200.
    try:
        _props_probe = _json.loads(body.properties) if body.properties.strip() != "{}" else {}
    except Exception:
        _props_probe = {}
    _refusal = soul_key_refusal(_props_probe)
    if _refusal:
        return _decode(_refusal)

    try:
        props = _json.loads(body.properties) if body.properties.strip() != "{}" else {}

        # 1. Node validation — type allow-list, property schema, enums. The
        #    registry is loaded once and both validators are pure over it:
        #    the old shape ran a SELECT per answer and spent up to six round
        #    trips validating one node and one edge, which cost nothing
        #    against a file.
        registry = await load_registry()
        ok, code, detail = validate_node(
            body.entity_type, props, registry=registry)
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
                registry=registry,
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

        disambig = await _call(
            check_disambiguation,
            body.name,
            body.entity_type,
            caller.mind_for_write(body.mind_id, required=False),
            caller.owner_for_write(body.owner),
        )
        if disambig.action == "disambiguate":
            await _call(
                send_disambiguation_message, body.name, disambig.existing_nodes)
            return {
                "ok": False,
                "code": "disambiguation_required",
                "detail": f"existing node(s) match {body.name!r}",
                "similar_nodes": disambig.existing_nodes,
            }

        # 4. Delegate to the existing write path.
        write_result = _decode(
            await graph_upsert_direct(
                entity_type=body.entity_type,
                name=body.name,
                properties=body.properties,
                relation=body.relation,
                target_name=body.target_name,
                target_type=body.target_type,
                edge_attrs=body.edge_attrs,
                mind_id=caller.mind_for_write(body.mind_id, required=False),
                owner=caller.owner_for_write(body.owner),
                stamp=caller.owner_to_stamp(body.owner),
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
    except HTTPException:
        raise
    except hive_data.HiveDataError as refused:
        log.warning("graph_upsert refused by the store: %s", refused)
        raise _store_http(refused) from refused
    except Exception as e:
        log.exception("graph_upsert failed")
        return {"ok": False, "code": "internal_error", "detail": str(e)}


@router.get("/schema")
async def get_schema() -> dict:
    """Return the full schema allow-list.

    Read by callers (typically minds) before they construct a write so they
    can choose a valid type/edge and avoid round-trips on rejection.
    """
    from hive_data import graph as store

    types = [
        {"name": r["name"], "kind": r["kind"],
         "schema": json.loads(r["property_schema"])}
        for r in await store.types_all()
    ]
    edges = [
        {"name": r["name"], "source_type": r["source_type"],
         "target_type": r["target_type"],
         "schema": json.loads(r["attr_schema"]),
         "symmetric": bool(r["symmetric"])}
        for r in await store.edge_types_all()
    ]
    return {"types": types, "edges": edges}


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
async def add_schema_type(body: SchemaTypeBody) -> dict:
    """Insert a new node type into the allow-list.

    Bearer-gated by ``LUCENT_ADMIN_BEARER_TOKEN``. Only hive-tools (which
    fronts the request-new-type HITL flow) holds this token.
    """
    import time

    from hive_data import graph as store

    if body.kind not in ("first-class", "second-class"):
        return {"ok": False, "code": "invalid_kind", "detail": "kind must be first-class or second-class"}

    try:
        written = await store.type_put(
            name=body.name, kind=body.kind,
            property_schema=body.property_schema, created_at=time.time())
    except hive_data.HiveDataError as refused:
        return {"ok": False, "code": "duplicate_or_db_error",
                "detail": f"{type(refused).__name__}: {refused}"}
    if not int(written.get("rows", 0)):
        # `ON CONFLICT DO NOTHING` wrote nothing, which here means the name
        # is already registered. Reported rather than answered `ok`: the
        # caller asked for a type it does not have and would otherwise write
        # against the one that is there.
        return {"ok": False, "code": "duplicate_or_db_error",
                "detail": f"node type {body.name!r} is already registered"}
    return {"ok": True, "name": body.name}


@router.post("/schema/edges", dependencies=[Depends(require_admin_bearer)])
async def add_schema_edge(body: SchemaEdgeBody) -> dict:
    """Insert a new edge type into the allow-list. Bearer-gated."""
    import time

    from hive_data import graph as store

    try:
        written = await store.edge_type_put(
            name=body.name, source_type=body.source_type,
            target_type=body.target_type, attr_schema=body.attr_schema,
            symmetric=1 if body.symmetric else 0, created_at=time.time())
    except hive_data.HiveDataError as refused:
        return {"ok": False, "code": "duplicate_or_db_error",
                "detail": f"{type(refused).__name__}: {refused}"}
    if not int(written.get("rows", 0)):
        return {"ok": False, "code": "duplicate_or_db_error",
                "detail": f"relationship {body.name!r} is already registered"}
    return {"ok": True, "name": body.name}


@router.post("/upsert-direct")
async def graph_upsert_direct_endpoint(
    body: UpsertBody, caller: Caller = Depends(require_holder)
) -> Any:
    """Bypass guards and write directly. Use only when the caller has
    already validated the entity (e.g., during ingestion replays)."""
    from lucent_api.lucent_graph import graph_upsert_direct

    return _decode(
        await graph_upsert_direct(
            entity_type=body.entity_type,
            name=body.name,
            properties=body.properties,
            relation=body.relation,
            target_name=body.target_name,
            target_type=body.target_type,
            edge_attrs=body.edge_attrs,
            mind_id=caller.mind_for_write(body.mind_id, required=False),
            owner=caller.owner_for_write(body.owner),
            stamp=caller.owner_to_stamp(body.owner),
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
async def graph_nodes_create(
    body: NodeCreateBody, caller: Caller = Depends(require_holder)
) -> Any:
    """Create a new node. Rejects if a node with (name, type) already exists.

    For property edits on an existing node use /graph/properties/merge.
    """
    from lucent_api.lucent_graph import graph_node_create

    return _decode(
        await graph_node_create(
            entity_type=body.entity_type,
            name=body.name,
            data_class=body.data_class,
            mind_id=caller.mind_for_write(body.mind_id, required=False),
            owner=caller.owner_for_write(body.owner),
            stamp=caller.owner_to_stamp(body.owner),
            properties=body.properties,
            as_of=body.as_of,
            source=body.source,
        )
    )


async def _refuse_foreign_identity_node(
    caller: Caller, entity_type: str, name: str
) -> None:
    """Refuse a property write onto another mind's own node, with a 403.

    A node belongs to a person, so one of Daniel's minds adding a fact to a
    node another of his minds wrote is the hive working — the authorship
    check this replaces refused exactly that, which is why a mind could not
    enrich a Person node it had not created itself. What a mind may never
    touch is the one node that *is* another mind: a `Mind` node carries that
    mind's soul, and nothing in an owner's graph is a stronger claim about
    somebody else.

    The rule itself is `lucent_graph._check_identity_guard`, which these
    routes' underlying functions also apply on their own — a direct call to
    `graph_properties_merge`, which is how this module is registered as a
    tool, used to have no identity guard at all. What is left here is the
    *status*: this path answers 403 rather than 200 with a code, and it did
    so before the move.

    Reach across owners is handled a layer down, by the owner filter every
    lookup carries, which answers as absence rather than as a refusal.

    An admin and a service are unrestricted: the console's soul editor and
    the operator are how a wedged identity gets fixed.
    """
    if caller.may_act_for_another_mind:
        return
    if entity_type != "Mind":
        return
    from lucent_api.lucent_graph import _check_identity_guard

    # Every spelling of itself this holder may use, because a mind minted
    # under a readable name whose rows are keyed by uuid answers to both and
    # the guard compares one string.
    refusals = [
        await _check_identity_guard(name, own, entity_type="Mind")
        for own in (caller.own_names or {caller.provenance})
    ]
    if any(r is None for r in refusals):
        return
    raise HTTPException(
        403,
        f"{name!r} is another mind's own node; {caller.holder!r} may read it "
        "and not change it",
    )


@router.post("/properties/merge")
async def graph_properties_merge_endpoint(
    body: PropertiesMergeBody, caller: Caller = Depends(require_holder)
) -> Any:
    """Merge properties into an existing node. Keys not in the request are
    preserved — the additive counterpart to the full-replace upsert.
    """
    await _refuse_foreign_identity_node(caller, body.entity_type, body.name)
    from lucent_api.lucent_graph import graph_properties_merge

    return _decode(
        await graph_properties_merge(
            entity_type=body.entity_type,
            name=body.name,
            properties=body.properties,
            owner=caller.owner_for_write(body.owner),
            as_of=body.as_of,
            source=body.source,
            mind_id=caller.provenance,
            unrestricted=caller.may_act_for_another_mind,
        )
    )


@router.post("/properties/remove")
async def graph_properties_remove_endpoint(
    body: PropertiesRemoveBody, caller: Caller = Depends(require_holder)
) -> Any:
    """Remove named keys from an existing node's properties blob.

    Protected metadata (name, type, created_at, updated_at, as_of,
    superseded) cannot be removed via this path. Vector-store columns
    (`data_class`, `tier`, `source`) ARE removable — they leak onto KG
    nodes from the shared write pipeline and the tidy walk strips them.
    """
    await _refuse_foreign_identity_node(caller, body.entity_type, body.name)
    from lucent_api.lucent_graph import graph_properties_remove

    return _decode(
        await graph_properties_remove(
            entity_type=body.entity_type,
            name=body.name,
            keys=body.keys,
            owner=caller.owner_for_write(body.owner),
            mind_id=caller.provenance,
            unrestricted=caller.may_act_for_another_mind,
        )
    )


@router.post("/edges")
async def graph_edges_create(
    body: EdgeBody, caller: Caller = Depends(require_holder)
) -> Any:
    """Create an edge between two existing nodes. Neither endpoint's
    properties are touched. Both nodes must already exist.
    """
    from lucent_api.lucent_graph import graph_edge_create

    return _decode(
        await graph_edge_create(
            source_name=body.source_name,
            source_type=body.source_type,
            target_name=body.target_name,
            target_type=body.target_type,
            relation=body.relation,
            edge_attrs=body.edge_attrs,
            data_class=body.data_class,
            mind_id=caller.mind_for_write(body.mind_id, required=False),
            owner=caller.owner_for_write(body.owner),
            stamp=caller.owner_to_stamp(body.owner),
            as_of=body.as_of,
            source=body.source,
        )
    )


@router.delete("/edges")
async def graph_edges_delete(
    body: EdgeBody, caller: Caller = Depends(require_holder)
) -> Any:
    """Delete edge(s) matching the (source, relation, target) tuple."""
    from lucent_api.lucent_graph import graph_edge_delete

    return _decode(
        await graph_edge_delete(
            source_name=body.source_name,
            source_type=body.source_type,
            target_name=body.target_name,
            target_type=body.target_type,
            relation=body.relation,
            owner=caller.owner_for_write(body.owner),
        )
    )


@router.delete("/nodes")
async def graph_nodes_delete(
    body: NodeDeleteBody, caller: Caller = Depends(require_holder)
) -> Any:
    """Hard-delete a node and every edge touching it. Use for true
    duplicates or write-mistakes; for facts that became stale prefer
    setting ``superseded = true`` via /properties/merge.
    """
    from lucent_api.lucent_graph import graph_node_delete

    return _decode(
        await graph_node_delete(
            entity_type=body.entity_type,
            name=body.name,
            owner=caller.owner_for_write(body.owner),
            mind_id=caller.provenance,
            unrestricted=caller.may_act_for_another_mind,
        )
    )


# ---- Maintenance endpoints ----


@router.post("/persons/audit")
async def audit_persons(
    body: AuditPersonsBody, caller: Caller = Depends(require_holder)
) -> Any:
    """Audit Person nodes for missing first/last name properties."""
    from lucent_api.lucent_graph import audit_person_nodes

    return _decode(
        await audit_person_nodes(owner=caller.owner_for_write(body.owner)))


@router.post("/persons/update-names")
async def update_person_names(
    body: UpdatePersonNamesBody, caller: Caller = Depends(require_holder)
) -> Any:
    """Backfill first_name/last_name on a Person node."""
    from lucent_api.lucent_graph import update_person_names as _update_names

    return _decode(
        await _update_names(
            name=body.name,
            first_name=body.first_name,
            last_name=body.last_name,
            owner=caller.owner_for_write(body.owner),
        )
    )


@router.get("/raw-properties")
async def graph_raw_properties(
    name: str = Query(...),
    mind_id: str = Query(""),
    caller: Caller = Depends(require_holder),
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
    from lucent_api.lucent_graph import NODES, _one, _owners_scope

    own_clause, own_params = _owners_scope(caller.owners_for_read(), next_arg=2)
    try:
        row = await _one(
            f"SELECT type, properties, mind_id FROM {NODES} "
            "WHERE LOWER(name) = LOWER($1)" + own_clause + " LIMIT 1",
            name, *own_params)
    except hive_data.HiveDataError as refused:
        raise _store_http(refused) from refused
    if row is None:
        return {"found": False}
    try:
        blob = json.loads(row["properties"]) if row["properties"] else {}
    except (TypeError, ValueError):
        blob = {}
    # mind_id is writer-provenance — surface only on Mind nodes.
    result: dict[str, Any] = {"found": True, "properties": blob}
    if row["type"] == "Mind":
        result["mind_id"] = row["mind_id"]
    return result


@router.get("/data")
async def graph_data(
    limit: int = Query(400, ge=1, le=2000),
    caller: Caller = Depends(require_holder),
) -> dict:
    """Visualization export — flat nodes + edges.

    Returns up to `limit` nodes (ordered by id) plus all edges that connect
    pairs within that node set. Used by Spark-to-Bloom's Cytoscape view.
    """
    from hive_data import graph as store
    from lucent_api.lucent_graph import NODES, _decoded, _owners_scope, _rows

    # `WHERE 1` is SQLite's. Postgres wants a boolean, so the clause this
    # fragment is appended to has to start from one.
    own_clause, own_params = _owners_scope(caller.owners_for_read(), next_arg=1)
    try:
        node_rows = await _rows(
            f"SELECT id, name, type, properties FROM {NODES} WHERE TRUE"
            + own_clause + f" ORDER BY id LIMIT ${1 + len(own_params)}",
            *own_params, limit)
        edge_rows = await store.edges_for_nodes(
            node_ids=[r["id"] for r in node_rows])
    except hive_data.HiveDataError as refused:
        raise _store_http(refused) from refused
    nodes = []
    for r in node_rows:
        props = _decoded(r["properties"])
        nodes.append(
            {
                "id": r["id"],
                "label": r["name"],
                "type": r["type"],
                **{k: v for k, v in props.items() if k not in ("id", "label", "type")},
            }
        )
    node_ids = {r["id"] for r in node_rows}
    edges = [
        {"source": r["source_id"], "target": r["target_id"], "type": r["type"]}
        for r in edge_rows
        if r["source_id"] in node_ids and r["target_id"] in node_ids
    ]
    return {"nodes": nodes, "edges": edges}
