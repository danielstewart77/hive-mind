"""Lucent knowledge graph tools, over the hive's one Postgres.

Provides the function signatures and JSON return shapes the MCP
``knowledge_graph.py`` had, backed by `hive_data` rather than by a SQLite
file. Every function is ``async`` now: there is no synchronous face on the
access layer by design, and a bridge written here would be the one place in
the hive that blocks an event loop on a database call.

Node types and relationships are the registry's, read from
``memory.schema_types`` / ``memory.schema_edges`` at every call.

Designed for direct FastMCP registration (no @tool() decorator) — which is
why the guards live *here* and not only in the router. A direct call to
`graph_properties_merge` used to have no identity guard at all, because the
only one was at the route.

What the move off the file changed, and had to be written rather than ported:

* ``IS NOT DISTINCT FROM`` in `_owner_scope`. SQLite's ``IS`` is Postgres'
  ``IS NOT DISTINCT FROM``, and `= $1` is not a repair: a Mind node is
  deliberately ownerless until its credential is minted, and `= NULL`
  matches nothing.
* ``AND FALSE`` in `_owners_scope`. ``AND 0`` is not a boolean here, so a
  mechanical port turns the fail-closed branch into a type error — and
  deleting the clause turns it into a total owner-partition bypass.
* ``LOWER()`` on both sides of every ``LIKE``. SQLite's ``LIKE`` is
  case-insensitive and Postgres' is not, so an unwrapped pattern reports
  "nothing matched" over rows that do.
* The database's own refusals. ``Conflict``, ``MissingReference``,
  ``ValueRefused``, ``TimedOut``, ``NoConnectionAvailable`` and
  ``Unreachable`` are routine against a socket where they were impossible
  against a file. Each arrives as a refusal that names the rule that refused
  it, carrying the status the router should answer with — a refusal that
  returns 200 is a refusal nobody notices.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

import hive_data
from hive_data import graph as store
from hive_data import UNSET

from lucent_api.memory_schema import validate_source
from lucent_api.soul import SOUL_KEY_REFUSED, preserve_soul, soul_key_refusal

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8420")
HITL_TTL = 180

#: Allow-list lives in the `schema_types` table (single runtime source of
#: truth). Register new types by POSTing /graph/schema/types — the validator
#: picks them up immediately without a restart.
_RELATION_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

NODES = 'memory."nodes"'
EDGES = 'memory."edges"'

#: The node columns every read of this module names. Never ``SELECT *``: a
#: column added to the table arrives in a properties blob the caller then
#: round-trips back into a write.
_NODE_FIELDS = (
    'id, name, type, first_name, last_name, properties, mind_id, owner, '
    'data_class, tier, source, as_of, created_at, updated_at'
)


async def _rows(sql: str, *args: Any) -> list[dict]:
    """Every row a statement answers with, as plain dicts."""
    records = await hive_data.run(lambda c: c.fetch(sql, *args))
    return [dict(r) for r in records]


async def _one(sql: str, *args: Any) -> dict | None:
    """One row, or None."""
    record = await hive_data.run(lambda c: c.fetchrow(sql, *args))
    return None if record is None else dict(record)


def _decoded(raw: object) -> dict:
    """A properties blob as a dict, treating unreadable as empty.

    `properties` is a `text` column, so a row written by something else — a
    load, a hand-edit, an older schema — is a blob that will not parse. One
    of those used to take a whole read down: `audit_person_nodes` answered
    `{"error": ...}` over a graph whose other four hundred Person nodes were
    fine, and the operator reads that as a broken audit rather than as one
    bad row.
    """
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


async def _call(fn, *args, **kwargs):
    """Call a collaborator that may be a coroutine function or a blocking one.

    `soul.preserve_soul`, `schema_registry.validate_node` and the `kg_guards`
    pair are all other modules' and all read the database, so all of them
    become coroutines on this cutover. A blocking one is run off the loop
    rather than on it: `send_disambiguation_message` posts to Telegram, and a
    Telegram that has stopped answering would otherwise stall `/health` and
    every other route for the length of its timeout.
    """
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    result = await asyncio.to_thread(fn, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


# ---- Refusals -------------------------------------------------------------
#
# Over a file, `except Exception -> {"code": "internal_error"}` at HTTP 200
# was defensible: the failures it could catch were bugs. Over a socket the
# database refuses routinely, and each refusal has a different remedy — a
# duplicate row, an absent parent, a value the column will not take, a
# statement that ran out of time, a pool with nothing free, a server that is
# not there. Folding the six into one code sends the operator to the wrong
# problem, and answering 200 means nothing notices at all.

#: Each database refusal, as the code and HTTP status it reaches a caller as.
_STORE_REFUSALS: tuple[tuple[type[Exception], str, int], ...] = (
    (hive_data.Conflict, "conflict", 409),
    (hive_data.MissingReference, "missing_reference", 409),
    (hive_data.ValueRefused, "value_refused", 400),
    (hive_data.TransactionAborted, "store_aborted", 409),
    (hive_data.SequenceBehind, "store_sequence_behind", 500),
    (hive_data.NoConnectionAvailable, "store_busy", 503),
    (hive_data.TimedOut, "store_timeout", 503),
    (hive_data.Unreachable, "store_unreachable", 503),
    (hive_data.CredentialRefused, "store_credential_refused", 503),
    (hive_data.DatabaseMissing, "store_database_missing", 503),
    (hive_data.NotInitialised, "store_not_open", 503),
    (hive_data.NotConfigured, "store_not_configured", 503),
    (hive_data.SchemaMismatch, "store_schema_mismatch", 503),
)


def _store_refusal(exc: BaseException, *, doing: str,
                   extra: dict | None = None) -> dict | None:
    """What a database refusal is called, or None if this is not one.

    ``http_status`` travels in the body because these functions return JSON
    strings to a router that has to decide a status from them, and because
    the same functions are registered directly as tools where the body is
    all there is.
    """
    for kind, code, status in _STORE_REFUSALS:
        if isinstance(exc, kind):
            logger.warning("graph.%s refused by the store: %s", doing, exc)
            return {
                "ok": False,
                "code": code,
                "detail": f"{doing}: {exc}",
                "http_status": status,
                **(extra or {}),
            }
    return None


def _failed(exc: BaseException, *, doing: str, extra: dict | None = None) -> str:
    """The payload any exception out of one of these functions becomes."""
    refusal = _store_refusal(exc, doing=doing, extra=extra)
    if refusal is not None:
        return json.dumps(refusal)
    logger.exception("graph.%s failed", doing)
    return json.dumps({
        "ok": False, "code": "internal_error", "detail": str(exc),
        **(extra or {}),
    })


async def _hitl_gate(summary: str) -> bool:
    """Request HITL approval showing the exact graph operation to be written.

    Awaited rather than blocking. This waits up to three minutes for a human,
    and a `requests.post` on an event loop holding every other route and
    `/health` hostage for that long is not a gate, it is an outage.
    """
    try:
        async with httpx.AsyncClient(timeout=HITL_TTL + 5) as client:
            resp = await client.post(
                f"{GATEWAY_URL}/hitl/request",
                json={"action": "graph_upsert", "summary": summary,
                      "ttl": HITL_TTL},
            )
            resp.raise_for_status()
            return bool(resp.json().get("approved", False))
    except Exception:
        return False


async def all_type_names() -> list[str]:
    """Every registered node type, by name."""
    return [str(r["name"]) for r in await store.types_all()]


async def _validate_label(entity_type: str) -> str:
    if await store.type_spec(name=entity_type) is None:
        raise ValueError(
            f"Invalid entity_type {entity_type!r}. "
            f"Must be one of: {await all_type_names()}"
        )
    return entity_type


def _validate_relation(relation: str) -> str:
    if not _RELATION_RE.match(relation):
        raise ValueError(
            f"Invalid relation {relation!r}. Must be uppercase letters/underscores."
        )
    return relation


def _identity_scope(label: str, owner: object) -> object:
    """The owner a lookup for this node type should be scoped to.

    Every lookup is scoped to one person's graph except one: finding the
    node that *is* a mind. A mind whose node this store cannot yet resolve
    to an owner holds it under a null owner, and a scoped lookup misses that
    row — so the mind's own next write does not find its node, inserts a
    second one, and `graph_query`, which keys its answer on the name, serves
    the empty newcomer while the soul-bearing original sits in the table. A
    mind's name is unique across the hive and published in the broker's own
    listing, so looking one up unscoped reveals nothing and prevents a fork.
    """
    return None if label == "Mind" else owner


def _owner_scope(owner: object, column: str = "owner", *,
                 next_arg: int = 1) -> tuple[str, list]:
    """A fragment scoping one statement to one person's graph.

    ``None`` means every owner, which is the console and the hive's own
    services. Anything else is one person, and a statement carrying this
    cannot reach another's row — including the lookup that resolves a name
    to a row id, which without it picks between two owners holding one name
    by whichever id is lower. The guard and the write that follows it have
    to resolve the same way or the check passes on one row and the write
    lands on another.

    ``IS NOT DISTINCT FROM``, not ``IS`` and not ``=``. SQLite spells the
    null-safe comparison ``IS``, which is a syntax error here; and ``= $1``
    is not the repair, because a Mind node is deliberately ownerless until
    its credential is minted and ``= NULL`` matches nothing at all.

    ``next_arg`` is which placeholder number this fragment's parameter takes.
    Postgres numbers its parameters, so a fragment appended to a statement
    cannot spell its own ``?`` and has to be told where it lands.
    """
    if owner is None:
        return "", []
    return f" AND {column} IS NOT DISTINCT FROM ${next_arg}", [owner]


def _owners_scope(owners: object, column: str = "owner", *,
                  next_arg: int = 1) -> tuple[str, list]:
    """The read-side counterpart: several owners, or every one of them.

    An empty list is a recognised caller whose credential names no user. It
    reaches nothing rather than everything — spelled ``AND FALSE``, because
    SQLite's ``AND 0`` is "argument of AND must be type boolean" here. The
    tempting repair is to emit no clause at all, which is not a smaller bug
    than the type error: it is every owner's graph served to a caller
    entitled to none of it.

    One placeholder whatever the number of owners: ``= ANY($n::text[])``
    takes the list as a single array parameter.
    """
    if owners is None:
        return "", []
    names = [str(o) for o in owners]
    if not names:
        return " AND FALSE", []
    return f" AND {column} = ANY(${next_arg}::text[])", [names]


async def _find_node(*, type: str, name: str, owner: object) -> dict | None:
    """One node by type and name, within one owner's graph or across all.

    ``owner`` of ``None`` is every owner — the console and the hive's own
    services — and is passed through to the access layer as "unset" rather
    than as a null owner, which is a different and much smaller set of rows.
    """
    if owner is None:
        return await store.node_find(type=type, name=name)
    return await store.node_find(type=type, name=name, owner=owner)


async def _check_identity_guard(
    name: str,
    mind_id: str,
    owner: object = None,
    entity_type: str = "Mind",
    unrestricted: bool = False,
) -> str | None:
    """Return an error string if the caller may not write to this node.

    A node is an identity node when its ``type`` is ``Mind``, and only the
    mind that node represents may change it. Returns None when the write is
    allowed: the request is not about a Mind node, the caller is that mind,
    no Mind node of the name exists yet, or the caller is the console or the
    operator.

    ``entity_type`` is the type the *request* names, and it is load-bearing:
    asked about a Person called "Hex" this used to answer for the Mind node
    that happens to share the name, which made every node named after a mind
    untouchable by everybody.

    ``unrestricted`` is the admin and service credential. The console's
    identity editor and the operator are how a wedged soul gets fixed, so
    they are not held to a rule about which mind is which.

    ``owner`` is accepted and deliberately not used in the lookup; see below.
    """
    if unrestricted:
        return None
    if entity_type != "Mind":
        return None
    # Deliberately NOT owner-scoped, and the only lookup in this module that
    # is not. A mind whose own node this store cannot yet resolve to an owner
    # holds it under a null owner — both of the boys' minds do, until their
    # credentials are minted here — and a scoped lookup misses that row, finds
    # no identity node, refuses nothing, and lets the mind mint a *second*
    # node of its own name. `graph_query` keys its answer on the name, so the
    # empty newcomer then shadows the soul-bearing original on every read
    # while the original sits in the table untouched. A fork is a worse
    # outcome than a refusal.
    #
    # The cost is that naming another owner's mind answers with a refusal
    # rather than with absence. Mind names are not private — every mind reads
    # the broker's own listing of them — so this leaks nothing the hive does
    # not already publish.
    #
    # `LOWER()` folds only ASCII in SQLite and the whole of Unicode in
    # Postgres, so this now matches pairs of names it used to let past — a
    # Turkish dotless i against an ASCII one, say. That is the safe
    # direction: the new matches are refusals, never permissions.
    row = await _one(
        f"SELECT mind_id FROM {NODES} "
        "WHERE LOWER(name) = LOWER($1) AND type = 'Mind' LIMIT 1",
        name,
    )
    if row is None:
        return None  # no identity node of that name anywhere yet
    if row["mind_id"] == mind_id:
        return None  # caller is the mind this node represents
    return f"mind_id {mind_id!r} cannot edit identity node for {name!r}"


def _identity_refusal(detail: str) -> str:
    """The identity guard's refusal, in the shape every caller reads."""
    return json.dumps({
        "ok": False, "code": "identity_guard", "detail": detail,
    })


async def graph_upsert_direct(
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
    owner: str | None,
    # Which owner a row this call *creates* is filed under. The scope above
    # may be "every owner", which is not something a row can be.
    stamp: str | None = None,
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

        label = await _validate_label(entity_type)
        props = json.loads(properties) if properties.strip() != "{}" else {}
        refusal = soul_key_refusal(props)
        if refusal:
            return refusal
        props.update(meta)
        props["created_at"] = time.time()

        # REQ-008: identity-node guard. A mind can only write its own identity node.
        guard_err = await _check_identity_guard(
            name, mind_id, owner, entity_type=label
        )
        if guard_err is not None:
            return json.dumps({"upserted": False, "error": guard_err})
        # The relation target is a second node this call writes to, and the
        # find-or-create below updates its columns. Unguarded, naming another
        # mind's own node as the target of any edge rewrote that node's tier
        # and data class — standing identity down to contextual, which drops
        # it out of the retrieval it exists for.
        tgt_label = (
            await _validate_label(target_type or entity_type)
            if relation and target_name else ""
        )
        if relation and target_name:
            target_guard = await _check_identity_guard(
                target_name, mind_id, owner, entity_type=tgt_label,
            )
            if target_guard is not None:
                return json.dumps({"upserted": False, "error": target_guard})
        # A row is filed under one owner. `owner` is the scope a call may
        # reach, which for the console and the operator is every owner.
        _stamped = stamp if stamp is not None else owner

        # Extract first_name / last_name from properties if provided
        first_name = props.pop("first_name", None)
        last_name = props.pop("last_name", None)

        await _call(preserve_soul, name=name, label=label, props=props,
                    owner=_identity_scope(label, owner))

        # `memory_nodes_owner_type_name` is a real unique index now, so the
        # old find-then-UPDATE-or-INSERT is a race that loses one caller's
        # write behind a duplicate-key error — and under a null owner, which
        # is how a Mind node waits for its credential, the index does not
        # collide at all and the node simply forks in two. `node_write` holds
        # the lock and the find and the write in one transaction.
        existing = await _find_node(
            type=label, name=name, owner=_identity_scope(label, owner))
        row = await store.node_write(
            owner=existing["owner"] if existing else _stamped,
            type=label, name=name, mind_id=mind_id, properties=props,
            first_name=UNSET if first_name is None else first_name,
            last_name=UNSET if last_name is None else last_name,
            data_class=meta.get("data_class"), tier=meta.get("tier"),
            source=meta.get("source", source), as_of=meta.get("as_of"),
            created_at=(existing or {}).get("created_at") or props["created_at"],
            updated_at=props["created_at"],
        )
        node_id = row["id"]

        rel_created = False
        if relation and target_name:
            rel_type = _validate_relation(relation)

            # Find-or-create the target node. Its properties are deliberately
            # not touched when it already exists: an edge is not a reason to
            # rewrite the node at its far end.
            existing_target = await _find_node(
                type=tgt_label, name=target_name,
                owner=_identity_scope(tgt_label, owner))
            if existing_target:
                target_id = existing_target["id"]
                await hive_data.run(lambda c: c.execute(
                    f"UPDATE {NODES} SET data_class = $1, tier = $2, "
                    "source = $3, as_of = $4, updated_at = $5 WHERE id = $6",
                    meta.get("data_class"), meta.get("tier"),
                    meta.get("source", source), meta.get("as_of"),
                    props["created_at"], target_id))
            else:
                target_row = await store.node_write(
                    owner=_stamped, type=tgt_label, name=target_name,
                    mind_id=mind_id, properties=meta,
                    data_class=meta.get("data_class"), tier=meta.get("tier"),
                    source=meta.get("source", source), as_of=meta.get("as_of"),
                    created_at=props["created_at"],
                    updated_at=props["created_at"])
                target_id = target_row["id"]

            edge_props = json.loads(edge_attrs) if edge_attrs.strip() else {}
            try:
                await store.edge_write(
                    mind_id=mind_id, source_id=node_id, target_id=target_id,
                    type=rel_type, owner=_stamped, properties=edge_props,
                    as_of=meta.get("as_of"), source=meta.get("source", source),
                    data_class=meta.get("data_class"), tier=meta.get("tier"),
                    created_at=props["created_at"])
            except hive_data.Conflict:
                # The edge is already there. Upsert is the contract on this
                # route, so its attributes are refreshed rather than refused.
                await hive_data.run(lambda c: c.execute(
                    f"UPDATE {EDGES} SET as_of = $1, source = $2, "
                    "data_class = $3, tier = $4, properties = $5 "
                    "WHERE source_id = $6 AND target_id = $7 AND type = $8",
                    meta.get("as_of"), meta.get("source", source),
                    meta.get("data_class"), meta.get("tier"),
                    json.dumps(edge_props, separators=(",", ":")),
                    node_id, target_id, rel_type))
            except hive_data.MissingReference as absent:
                # `memory.edges.mind_id` is a real foreign key into
                # `core.minds` and `memory.nodes.mind_id` is not, so this
                # call's two halves fail differently: the node landed and the
                # edge did not. Named, with the mind in it, rather than
                # reported as `internal_error` — a mind that is not
                # registered with the gateway is an operator action, and
                # "internal error" does not name it.
                return json.dumps({
                    "ok": False,
                    "upserted": True,
                    "id": node_id,
                    "entity_type": label,
                    "name": name,
                    "relation_created": False,
                    "code": "missing_reference",
                    "detail": (
                        f"the node {name!r} landed and the {relation} edge "
                        f"did not: {absent}"
                    ),
                    "http_status": 409,
                })
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
        refusal = _store_refusal(e, doing="upsert", extra={"upserted": False})
        if refusal is not None:
            refusal["error"] = refusal["detail"]
            return json.dumps(refusal)
        return json.dumps({"error": str(e)})


async def graph_upsert(
    *,
    entity_type: str,
    name: str,
    data_class: str,
    properties: str = "{}",
    relation: str = "",
    target_name: str = "",
    target_type: str = "",
    mind_id: str,
    owner: str | None,
    # Which owner a row this call *creates* is filed under. The scope above
    # may be "every owner", which is not something a row can be.
    stamp: str | None = None,
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
        mind_id: Which mind is writing. Recorded as provenance; it is not
            an ownership claim and does not scope anything but a Mind node.
        owner: Whose graph this node lives in. Every read and every write is
            scoped to it, and None is every owner — a service or an admin.
        data_class: Data class for this entry (e.g. "person", "preference", "technical-config").
        as_of: ISO datetime for when the fact was established (defaults to now).
        source: Origin of the entry -- "user", "tool", "session", or "self".

    Returns:
        JSON confirmation with node id and relationship created (if any).
    """
    try:
        from lucent_api.kg_guards import (
            check_disambiguation,
            check_orphan_guard,
            send_disambiguation_message,
        )

        label = await _validate_label(entity_type)
        props = json.loads(properties) if properties.strip() != "{}" else {}
        refusal = soul_key_refusal(props)
        if refusal:
            return refusal

        allowed, orphan_msg = check_orphan_guard(relation, target_name)
        if not allowed:
            return json.dumps({"upserted": False, "reason": orphan_msg})

        disambig = await _call(
            check_disambiguation, name, entity_type, mind_id, owner)
        if disambig.action == "disambiguate":
            await _call(send_disambiguation_message, name, disambig.existing_nodes)
            return json.dumps({
                "upserted": False,
                "reason": "disambiguation_required",
                "similar_nodes": disambig.existing_nodes,
            })

        await _call(preserve_soul, name=name, label=label, props=props,
                    owner=owner)

        props_str = f" {json.dumps(props)}" if props else ""
        node_repr = f"({label}:{name}{props_str})"
        if relation and target_name:
            tgt = target_type or entity_type
            hitl_summary = f"{node_repr} --[{relation}]--> ({tgt}:{target_name})"
        else:
            hitl_summary = node_repr

        if not await _hitl_gate(hitl_summary):
            return json.dumps({"upserted": False, "reason": "denied by HITL"})

        return await graph_upsert_direct(
            entity_type=entity_type,
            name=name,
            properties=properties,
            relation=relation,
            target_name=target_name,
            target_type=target_type,
            mind_id=mind_id,
            owner=owner,
            stamp=stamp,
            data_class=data_class,
            as_of=as_of,
            source=source,
        )
    except Exception as e:
        refusal = _store_refusal(e, doing="upsert", extra={"upserted": False})
        if refusal is not None:
            refusal["error"] = refusal["detail"]
            return json.dumps(refusal)
        return json.dumps({"error": str(e)})


#: The alias match, as a fragment. `properties` is a `text` column rather
#: than `jsonb`, so a cast is a statement that *raises* on a blob written by
#: something else — where SQLite's `json_extract` quietly answered NULL and
#: the row simply did not match. `pg_input_is_valid` inside a `CASE` is the
#: guard, and `CASE` rather than `AND` because only `CASE` promises the
#: planner will not reorder the cast ahead of the test.
_ALIAS_MATCH = (
    "LOWER(COALESCE(CASE WHEN pg_input_is_valid(properties, 'jsonb') "
    "THEN (properties::jsonb -> 'aliases')::text END, ''))"
)


def _json_text(path: str) -> str:
    """One key of the properties blob as text, guarded against a bad blob."""
    return (
        "LOWER(COALESCE(CASE WHEN pg_input_is_valid(properties, 'jsonb') "
        f"THEN properties::jsonb ->> '{path}' END, ''))"
    )


async def graph_query(
    entity_name: str,
    mind_id: str = "",
    depth: int = 1,
    owners: list[str] | None = None,
) -> str:
    """Retrieve knowledge graph node(s) and their connected relationships.

    Identity-only matching: name (exact, case-insensitive), first_name,
    last_name, or alias-element (substring within the JSON aliases list,
    bounded by JSON quotes for exact-element semantics).

    Property text mentions do NOT match. For mention search, use graph_search.

    Args:
        entity_name: Name to look up. Empty string returns no results.
        mind_id: Accepted for backward compatibility but ignored. A read is
            not partitioned by the mind that wrote the row: every one of an
            owner's minds reads the whole of that owner's graph, which is
            what makes it a hive rather than a set of private notebooks.
        owners: Whose graphs this read may see. None is every owner, which
            is a service or an admin. A node outside the list answers
            exactly as a node that is not there — the alternative is a
            name-guessing oracle across people.
        depth: How many hops to traverse (default 1, max 3).

    Returns:
        JSON with matching nodes, their properties, and connected relationships.
    """
    depth = min(max(depth, 1), 3)
    if not entity_name:
        return json.dumps({"found": False, "entity": entity_name})
    try:
        # Identity-only match: name / first_name / last_name (exact,
        # case-insensitive), or alias-element within the JSON aliases array.
        # Scoped by owner, never by the mind that wrote the row. The four
        # identity predicates are parenthesised: an owner clause ANDed onto a
        # bare OR chain binds to the last one only, which would serve every
        # owner's node whose name matched.
        alias_pattern = f'%"{entity_name}"%'
        own_clause, own_params = _owners_scope(owners, next_arg=3)
        rows = await _rows(
            f"""
            SELECT {_NODE_FIELDS}
            FROM {NODES}
            WHERE (LOWER(name) = LOWER($1)
               OR LOWER(COALESCE(first_name, '')) = LOWER($1)
               OR LOWER(COALESCE(last_name, '')) = LOWER($1)
               OR {_ALIAS_MATCH} LIKE LOWER($2))
            """ + own_clause,
            entity_name, alias_pattern, *own_params,
        )

        if not rows:
            return json.dumps({"found": False, "entity": entity_name})

        nodes: dict[str, dict] = {}
        walk_clause, walk_params = _owners_scope(owners, "n.owner", next_arg=2)
        for row in rows:
            node_name = row["name"]
            props = _decoded(row["properties"])
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
                    edges = await _rows(
                        """
                        SELECT e.type AS rel_type, e.target_id, e.source_id,
                               e.properties AS edge_props,
                               n.name AS connected_name, n.type AS connected_type,
                               n.properties AS connected_props,
                               n.first_name, n.last_name
                        FROM """ + EDGES + """ e
                        JOIN """ + NODES + """ n ON (
                            (e.target_id = n.id AND e.source_id = $1)
                            OR (e.source_id = n.id AND e.target_id = $1)
                        )
                        WHERE (e.source_id = $1 OR e.target_id = $1)
                        """ + walk_clause,
                        nid, *walk_params,
                    )

                    for edge in edges:
                        connected_id = (
                            edge["target_id"] if edge["source_id"] == nid
                            else edge["source_id"]
                        )

                        # Surface every edge — `visited` only gates BFS frontier
                        # expansion (cycle prevention), not edge surfacing. Two
                        # nodes may have multiple edges between them (e.g. both
                        # PARENT_OF and CHILD_OF, or FRIEND_OF and COWORKER_OF).
                        conn_props = _decoded(edge["connected_props"])
                        conn_props["name"] = edge["connected_name"]
                        conn_props["type"] = edge["connected_type"]
                        if edge["first_name"]:
                            conn_props["first_name"] = edge["first_name"]
                        if edge["last_name"]:
                            conn_props["last_name"] = edge["last_name"]

                        direction = "out" if edge["source_id"] == nid else "in"
                        edge_attrs = _decoded(edge["edge_props"])
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
        refusal = _store_refusal(e, doing="query")
        if refusal is not None:
            refusal["error"] = refusal["detail"]
            return json.dumps(refusal)
        return json.dumps({"error": str(e)})


async def graph_search(
    text: str, limit: int = 25, owners: list[str] | None = None
) -> str:
    """Mention search: full-text scan across all property strings.

    Returns nodes whose property values *mention* the query text. This is
    a separate concern from identity lookup (graph_query) — the return
    shape makes that explicit.

    Args:
        text: Substring to search for. Empty string returns an empty list.
        limit: Max number of results (default 25).
        owners: Whose graphs this read may see. None is every owner.

    Returns:
        JSON-encoded list of {"node_id", "node_type", "property", "snippet"}.
        One result per matching node — the first property hit wins.
    """
    if not text:
        return json.dumps([])
    try:
        # `LOWER()` on both sides. `LIKE` folds case in SQLite and does not
        # here, so the unwrapped pattern answered "no memories matched" over
        # rows that matched perfectly well — which is a sentence a model says
        # out loud.
        own_clause, own_params = _owners_scope(owners, next_arg=2)
        rows = await _rows(
            f"SELECT id, type, properties FROM {NODES} "
            "WHERE LOWER(COALESCE(properties, '')) LIKE LOWER($1)"
            + own_clause + f" LIMIT ${2 + len(own_params)}",
            f"%{text}%", *own_params, limit,
        )

        results: list[dict[str, object]] = []
        lower = text.lower()
        for row in rows:
            try:
                props = json.loads(row["properties"]) if row["properties"] else {}
            except json.JSONDecodeError:
                continue
            if not isinstance(props, dict):
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
        refusal = _store_refusal(e, doing="search")
        if refusal is not None:
            refusal["error"] = refusal["detail"]
            return json.dumps(refusal)
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


async def search_person(
    first_name: str = "",
    last_name: str = "",
    title: str = "",
    relationship: str = "",
    *,
    mind_id: str = "",
    owners: list[str] | None = None,
) -> str:
    """Search for Person nodes by any known name fragment or relationship.

    At least one of first_name/last_name/title/relationship must be provided.
    Matching is case-insensitive substring. Multiple params are combined with AND.

    Args:
        first_name: Given name fragment.
        last_name: Surname fragment.
        title: Title or honorific fragment.
        relationship: How this person relates to Daniel.
        mind_id: Accepted for backward compatibility but ignored. Every one
            of an owner's minds sees every Person node in that owner's graph.
        owners: Whose graphs to search. None is every owner, which is a
            service or an admin.

    Returns:
        JSON with matching Person nodes and their properties.
    """
    if not any([first_name, last_name, title, relationship]):
        return json.dumps({"error": "At least one search parameter must be provided."})

    try:
        # Build dynamic WHERE clause. Partitioned by owner, never by the
        # mind that wrote the row.
        conditions = ["type = 'Person'"]
        params: list[Any] = []
        own_clause, own_params = _owners_scope(owners, next_arg=1)
        if own_clause:
            conditions.append(own_clause.removeprefix(" AND "))
            params.extend(own_params)

        def _mark() -> str:
            return f"${len(params) + 1}"

        if first_name:
            conditions.append(
                f"LOWER(COALESCE(first_name, '')) LIKE LOWER({_mark()})")
            params.append(f"%{first_name}%")
        if last_name:
            conditions.append(
                f"LOWER(COALESCE(last_name, '')) LIKE LOWER({_mark()})")
            params.append(f"%{last_name}%")
        if title:
            # `COLLATE NOCASE` and `json_extract` are both SQLite's. The cast
            # is guarded, because `properties` is `text` and a blob written by
            # something else raises here where `json_extract` answered NULL.
            conditions.append(f"{_json_text('title')} LIKE LOWER({_mark()})")
            params.append(f"%{title}%")
        if relationship:
            # relationship is stored as a JSON array in properties
            conditions.append(
                f"{_json_text('relationship')} LIKE LOWER({_mark()})")
            params.append(f"%{relationship}%")

        where_clause = " AND ".join(conditions)
        rows = await _rows(
            f"SELECT {_NODE_FIELDS} FROM {NODES} WHERE {where_clause}",
            *params,
        )

        if not rows:
            return json.dumps({"found": False, "query": {
                "first_name": first_name,
                "last_name": last_name,
                "title": title,
                "relationship": relationship,
            }})

        matches = []
        for row in rows:
            props = _decoded(row["properties"])
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
        refusal = _store_refusal(e, doing="person-search")
        if refusal is not None:
            refusal["error"] = refusal["detail"]
            return json.dumps(refusal)
        return json.dumps({"error": str(e)})


async def audit_person_nodes(*, owner: object) -> str:
    """Find all Person nodes missing first_name or last_name properties.

    Scoped to one owner's graph rather than to one mind's writes. Keyed on
    the writer, this answered "nothing to fix" over every Person node one of
    the owner's *other* minds had written — a false clean bill of health,
    which reads exactly like a tidy graph.

    Args:
        owner: Whose graph to audit. None is every owner.

    Returns:
        JSON with found, count, and nodes list.
    """
    try:
        own_clause, own_params = _owner_scope(owner, next_arg=1)
        rows = await _rows(
            f"""
            SELECT id, name, first_name, last_name, properties
            FROM {NODES}
            WHERE type = 'Person'
              AND (first_name IS NULL OR last_name IS NULL)
            """ + own_clause,
            *own_params,
        )

        if not rows:
            return json.dumps({"found": False, "count": 0, "nodes": []})

        nodes_list = []
        for row in rows:
            props = _decoded(row["properties"])
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
        refusal = _store_refusal(e, doing="person-audit")
        if refusal is not None:
            refusal["error"] = refusal["detail"]
            return json.dumps(refusal)
        return json.dumps({"error": str(e)})


async def update_person_names(
    *,
    name: str,
    first_name: str = "",
    last_name: str = "",
    owner: object,
) -> str:
    """Update first_name and/or last_name on a Person node identified by name.

    Args:
        name: The existing name property of the Person node to update.
        first_name: Given name to set (omit or empty string to skip).
        last_name: Surname to set (omit or empty string to skip).
        owner: Whose graph the node lives in. None is every owner.

    Returns:
        JSON confirmation with updated status.
    """
    if not first_name and not last_name:
        return json.dumps({
            "error": "At least one of first_name or last_name must be provided."
        })

    try:
        set_parts = []
        params: list[Any] = []

        if first_name:
            params.append(first_name)
            set_parts.append(f"first_name = ${len(params)}")
        if last_name:
            params.append(last_name)
            set_parts.append(f"last_name = ${len(params)}")

        set_clause = ", ".join(set_parts)
        params.append(name)
        name_mark = f"${len(params)}"
        own_clause, own_params = _owner_scope(owner, next_arg=len(params) + 1)
        params.extend(own_params)

        # `RETURNING id` rather than a row count off the command tag: the
        # count is what decides whether the caller is told "not found", and
        # the rows themselves are the only reading of it that cannot be out
        # by one.
        changed = await _rows(
            f"UPDATE {NODES} SET {set_clause} "
            f"WHERE type = 'Person' AND name = {name_mark}{own_clause} "
            "RETURNING id",
            *params,
        )

        if not changed:
            return json.dumps({
                "updated": False,
                "reason": f"Person node not found with name={name!r}",
            })

        response: dict[str, object] = {"updated": True, "name": name}
        if first_name:
            response["first_name"] = first_name
        if last_name:
            response["last_name"] = last_name

        return json.dumps(response)
    except Exception as e:
        refusal = _store_refusal(e, doing="person-update")
        if refusal is not None:
            refusal["error"] = refusal["detail"]
            return json.dumps(refusal)
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


async def graph_node_create(
    *,
    entity_type: str,
    name: str,
    data_class: str,
    mind_id: str,
    owner: str | None,
    # Which owner a row this call *creates* is filed under. The scope above
    # may be "every owner", which is not something a row can be.
    stamp: str | None = None,
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

        label = await _validate_label(entity_type)
        props = json.loads(properties) if properties.strip() != "{}" else {}
        refusal = soul_key_refusal(props)
        if refusal:
            return refusal

        # A row is filed under one owner. `owner` is the scope a call may
        # reach, which for the console and the operator is every owner.
        _stamped = stamp if stamp is not None else owner
        existing = await _find_node(
            type=label, name=name, owner=_identity_scope(label, owner))
        if existing:
            return json.dumps({
                "ok": False,
                "code": "exists",
                "detail": f"node already exists: name={name!r} type={label!r}",
                "id": existing["id"],
            })

        guard_err = await _check_identity_guard(
            name, mind_id, owner, entity_type=label
        )
        if guard_err is not None:
            return _identity_refusal(guard_err)

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

        # A real INSERT rather than `node_write`, because this route's whole
        # contract is that it does not replace. `node_write` is the upsert,
        # and reaching for it here would turn a lost race into a silent
        # overwrite of whatever the winner had written.
        try:
            row = await _one(
                f"INSERT INTO {NODES} (mind_id, owner, type, name, first_name, "
                "last_name, properties, data_class, tier, source, as_of, "
                "created_at, updated_at) VALUES ($1, $2, $3, $4, $5, $6, $7, "
                "$8, $9, $10, $11, $12, $12) RETURNING id",
                mind_id, _stamped, label, name, first_name, last_name,
                json.dumps(props), meta["data_class"], meta["tier"],
                meta["source"], meta["as_of"], props["created_at"],
            )
        except hive_data.Conflict:
            # `memory_nodes_owner_type_name` caught what the check above
            # missed, which is a concurrent creator rather than a bug.
            # Reported as the same refusal: the caller asked to create a node
            # and one of that name is there.
            return json.dumps({
                "ok": False, "code": "exists",
                "detail": f"node already exists: name={name!r} type={label!r}",
            })
        return json.dumps({"ok": True, "id": row["id"], "name": name, "type": label})
    except Exception as e:
        return _failed(e, doing="node-create")


async def graph_properties_merge(
    *,
    entity_type: str,
    name: str,
    properties: str,
    owner: str | None,
    as_of: str | None = None,
    source: str | None = None,
    mind_id: str = "",
    unrestricted: bool = False,
) -> str:
    """Merge ``properties`` into an existing node's properties blob.

    Keys present in the request are set (overwriting prior values for
    those keys). Keys NOT in the request are preserved. This is the
    additive counterpart to the full-replace upsert.

    ``first_name`` and ``last_name`` in the request map to their columns
    rather than the properties blob, matching the existing storage shape.

    ``mind_id`` and ``unrestricted`` carry the identity guard. It is here
    and not only at the route because this module is registered directly as
    a tool: the route-level check guarded the route, and a direct call to
    this function had no identity guard at all.
    """
    try:
        label = await _validate_label(entity_type)
        new_props = json.loads(properties) if properties.strip() else {}
        refusal = soul_key_refusal(new_props)
        if refusal:
            return refusal
        if not isinstance(new_props, dict):
            return json.dumps({"ok": False, "code": "invalid_properties",
                               "detail": "properties must be a JSON object"})

        guard_err = await _check_identity_guard(
            name, mind_id, owner, entity_type=label, unrestricted=unrestricted
        )
        if guard_err is not None:
            return _identity_refusal(guard_err)

        row = await _find_node(type=label, name=name, owner=owner)
        if not row:
            return json.dumps({"ok": False, "code": "not_found",
                               "detail": f"node not found: name={name!r} type={label!r}"})

        # Split off column-backed keys.
        first_name = new_props.pop("first_name", None)
        last_name = new_props.pop("last_name", None)

        now_iso = as_of or datetime.now(timezone.utc).isoformat()
        touched = time.time()
        additions = dict(new_props)
        additions["updated_at"] = touched
        if source is not None:
            additions["source"] = source

        # `properties_merge` rather than a read, a decode, an update and a
        # write out here. `properties` is a `text` column, so the merge is
        # read-modify-write by construction, and two callers merging
        # different keys both read before either wrote — the loser's keys
        # vanish and both are told they landed. That operation holds the row.
        await store.properties_merge(node_id=row["id"], additions=additions)

        set_parts = ["updated_at = $1", "as_of = $2"]
        params: list[Any] = [touched, now_iso]
        if first_name is not None:
            params.append(first_name)
            set_parts.append(f"first_name = ${len(params)}")
        if last_name is not None:
            params.append(last_name)
            set_parts.append(f"last_name = ${len(params)}")
        if source is not None:
            params.append(source)
            set_parts.append(f"source = ${len(params)}")
        params.append(row["id"])
        await hive_data.run(lambda c: c.execute(
            f"UPDATE {NODES} SET {', '.join(set_parts)} "
            f"WHERE id = ${len(params)}", *params))

        return json.dumps({"ok": True, "id": row["id"],
                           "merged_keys": sorted(new_props.keys())})
    except Exception as e:
        return _failed(e, doing="properties-merge")


async def graph_properties_remove(
    *,
    entity_type: str,
    name: str,
    keys: list[str],
    owner: str | None,
    mind_id: str = "",
    unrestricted: bool = False,
) -> str:
    """Remove named keys from an existing node's properties blob.

    Rejects requests to remove protected metadata keys (as_of, created_at,
    updated_at, superseded, name, type). ``first_name`` / ``last_name`` in
    ``keys`` clear the corresponding column.

    ``mind_id`` and ``unrestricted`` carry the identity guard, for the same
    reason `graph_properties_merge` does.
    """
    try:
        label = await _validate_label(entity_type)
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

        guard_err = await _check_identity_guard(
            name, mind_id, owner, entity_type=label, unrestricted=unrestricted
        )
        if guard_err is not None:
            return _identity_refusal(guard_err)

        row = await _find_node(type=label, name=name, owner=owner)
        if not row:
            return json.dumps({"ok": False, "code": "not_found",
                               "detail": f"node not found: name={name!r} type={label!r}"})

        blob_keys = [k for k in keys if k not in _COLUMN_KEYS]
        dropped = await store.properties_remove(
            node_id=row["id"], keys=blob_keys)
        removed: list[str] = list(dropped["removed"])
        touched = time.time()
        await store.properties_merge(
            node_id=row["id"], additions={"updated_at": touched})

        set_parts = ["updated_at = $1"]
        params: list[Any] = [touched]
        if "first_name" in keys:
            set_parts.append("first_name = NULL")
            removed.append("first_name")
        if "last_name" in keys:
            set_parts.append("last_name = NULL")
            removed.append("last_name")
        # Vector-leakage keys may also live in their dedicated column — the
        # column NULL update here catches that. Either path counts.
        for col in _VECTOR_LEAKAGE_COLUMNS:
            if col in keys:
                set_parts.append(f"{col} = NULL")
                if col not in removed:
                    removed.append(col)
        params.append(row["id"])
        await hive_data.run(lambda c: c.execute(
            f"UPDATE {NODES} SET {', '.join(set_parts)} "
            f"WHERE id = ${len(params)}", *params))

        return json.dumps({"ok": True, "id": row["id"], "removed_keys": removed})
    except Exception as e:
        return _failed(e, doing="properties-remove")


async def graph_edge_create(
    *,
    source_name: str,
    source_type: str,
    target_name: str,
    target_type: str,
    relation: str,
    edge_attrs: str = "{}",
    data_class: str,
    mind_id: str,
    owner: str | None,
    # Which owner a row this call *creates* is filed under. The scope above
    # may be "every owner", which is not something a row can be.
    stamp: str | None = None,
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
        src_label = await _validate_label(source_type)
        tgt_label = await _validate_label(target_type)
        attrs = json.loads(edge_attrs) if edge_attrs.strip() != "{}" else {}

        # A row is filed under one owner. `owner` is the scope a call may
        # reach, which for the console and the operator is every owner.
        _stamped = stamp if stamp is not None else owner
        src = await _find_node(type=src_label, name=source_name, owner=owner)
        if not src:
            return json.dumps({"ok": False, "code": "source_not_found",
                               "detail": f"{src_label}:{source_name} not found"})
        tgt = await _find_node(type=tgt_label, name=target_name, owner=owner)
        if not tgt:
            return json.dumps({"ok": False, "code": "target_not_found",
                               "detail": f"{tgt_label}:{target_name} not found"})

        try:
            row = await store.edge_write(
                mind_id=mind_id, source_id=src["id"], target_id=tgt["id"],
                type=rel_type, owner=_stamped, properties=attrs,
                as_of=as_of or datetime.now(timezone.utc).isoformat(),
                source=source, data_class=data_class or "", tier="contextual",
                created_at=time.time())
        except hive_data.Conflict as clash:
            return json.dumps({
                "ok": False, "code": "edge_exists",
                "detail": (
                    f"edge already exists: {src_label}:{source_name} "
                    f"-[{rel_type}]-> {tgt_label}:{target_name}"
                ),
                "raw": str(clash),
            })
        # `MissingReference` is deliberately *not* caught here.
        # `memory.edges.mind_id` is a foreign key into `core.minds` and
        # `memory.nodes.mind_id` is not, so an edge can be refused for a
        # mind whose nodes write perfectly well — and `edge_write` already
        # raises with the mind in the sentence. `_failed` turns it into the
        # named refusal with its 409. A second handler here would be one
        # more place for that status to be lost.
        return json.dumps({"ok": True, "id": row["id"], "relation": rel_type})
    except Exception as e:
        return _failed(e, doing="edge-create")


async def _soul_bearing_node_refusal(
    entity_type: str, name: str, owner: object = None
) -> str | None:
    """Refuse a general-route delete of a Mind that still has a soul.

    An erasure leaves nothing behind to explain it: `soul_changes` records
    changes to a node, and the node is gone. Emptying the soul through
    POST /graph/soul first makes the loss visible in the record, and only
    then can the node go.

    A different set from "has soul history", deliberately: this reads the
    `soul_values` property, and a node can carry one without the other in
    either direction. `graph_node_delete` refuses on both.
    """
    if entity_type != "Mind":
        return None
    # The lookup's failures are not swallowed. Over a file this read could
    # fail only through a bug, and "could not check" quietly became "no soul
    # to lose"; over a socket it fails when the database does, and a guard
    # that opens on an outage is the one erasure nothing can explain
    # afterwards. `graph_node_delete` turns the refusal into a named one.
    row = await _find_node(type="Mind", name=name, owner=owner)
    if not row or not row["properties"]:
        return None
    try:
        props = json.loads(row["properties"])
    except (TypeError, ValueError):
        return None
    if isinstance(props, dict) and props.get("soul_values"):
        return json.dumps({
            "ok": False,
            "code": SOUL_KEY_REFUSED,
            "detail": (
                f"{name!r} still has a soul. Clear it through POST /graph/soul "
                "first, so the loss is recorded, then delete the node."
            ),
        })
    return None


async def graph_node_delete(
    *,
    entity_type: str,
    name: str,
    owner: str | None,
    mind_id: str = "",
    unrestricted: bool = False,
) -> str:
    """Hard-delete the node identified by (name, type) and every edge
    touching it (inbound or outbound). Returns counts. Use for true
    duplicates or write-mistakes; for facts that became stale prefer
    setting ``superseded = true`` via /properties/merge.
    """
    try:
        # Deleting a Mind node deletes its soul, unrecorded — the one erasure
        # the provenance table can never explain afterwards. Blocked here
        # rather than in the router because every caller of this function has
        # the same problem.
        refusal = await _soul_bearing_node_refusal(entity_type, name, owner)
        if refusal:
            return refusal

        label = await _validate_label(entity_type)
        # A mind's own node is the one row in its owner's graph that the
        # owner's other minds may read and not touch, and deleting it is the
        # loudest form of touching it.
        guard_err = await _check_identity_guard(
            name, mind_id, owner, entity_type=label, unrestricted=unrestricted,
        )
        if guard_err is not None:
            return _identity_refusal(guard_err)
        row = await _find_node(type=label, name=name, owner=owner)
        if not row:
            return json.dumps({"ok": False, "code": "not_found",
                               "detail": f"node not found: name={name!r} type={label!r}"})
        node_id = row["id"]
        try:
            # One unit for the edges and the node. `memory.soul_changes.node_id`
            # is ON DELETE RESTRICT, so the node delete can be refused *after*
            # the edges are gone — which leaves a node stripped of every
            # relationship and a caller told the delete failed.
            deleted = await store.node_delete(node_id)
        except hive_data.WriteRejected as refused:
            return json.dumps({
                "ok": False,
                "code": "soul_history_present",
                "detail": (
                    f"{name!r} has recorded soul changes, which are kept "
                    f"against the node they changed, so the node cannot be "
                    f"removed while they exist. Clear the soul through "
                    f"POST /graph/soul and remove its history before "
                    f"deleting. ({refused})"
                ),
                "http_status": 409,
            })
        return json.dumps({
            "ok": True,
            "id": node_id,
            "edges_deleted": deleted["edges_deleted"],
            "node_deleted": deleted["rows"],
        })
    except Exception as e:
        return _failed(e, doing="node-delete")


async def graph_edge_delete(
    *,
    source_name: str,
    source_type: str,
    target_name: str,
    target_type: str,
    relation: str,
    owner: str | None,
) -> str:
    """Delete the edge(s) matching (source_name, source_type) -[relation]->
    (target_name, target_type). Returns the count deleted.
    """
    try:
        rel_type = _validate_relation(relation)
        src_label = await _validate_label(source_type)
        tgt_label = await _validate_label(target_type)
        src = await _find_node(type=src_label, name=source_name, owner=owner)
        tgt = await _find_node(type=tgt_label, name=target_name, owner=owner)
        if not src or not tgt:
            return json.dumps({"ok": False, "code": "endpoint_not_found",
                               "detail": "source or target node missing"})
        removed = await store.edge_delete(
            source_id=src["id"], target_id=tgt["id"], type=rel_type)
        return json.dumps({"ok": True, "deleted": removed["rows"]})
    except Exception as e:
        return _failed(e, doing="edge-delete")


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
