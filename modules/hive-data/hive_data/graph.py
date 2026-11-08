"""The knowledge graph, the soul record, the type registry and the holders.

`memory.py` gave this package the memory side — store, recall, the vector and
word search. It gave it no node, edge, soul or registry operation at all, and
lucent's graph layer is ninety SQL call sites with nothing to call. This is
that half.

Three operations here are not ports. They are the places where SQLite's one
writer was silently supplying correctness that a pool does not:

* `node_write` replaces lucent's find-then-UPDATE-or-INSERT. Its comment says
  there is no database-level uniqueness on name and type — true of the file,
  false here, where `memory_nodes_owner_type_name` is a real unique index. Run
  twice at once, the old shape loses one caller's write behind a duplicate-key
  error, and under a NULL owner — which is how a Mind node waits for its
  credential to be minted — the plain btree does not collide at all and the
  node simply forks in two.
* `properties_merge` is a read, a decode, an update and a write, because
  `properties` is a text column rather than `jsonb`. Two callers merging
  different keys both read before either wrote, and the loser's keys vanish
  while both are told they landed.
* `soul_replace` is the node update and its provenance row. `soul.py` does
  them under one implicit SQLite transaction and its docstring promises the
  record cannot drift from what was overwritten; this package is autocommit
  per operation, so the promise has to be made explicitly or a soul is
  replaced with nothing recording what it used to be — and the
  compare-and-set that reads the change count then passes when it should
  refuse.

Policy is deliberately *not* here. The owner partition, the identity guard,
label validation and the human-in-the-loop gate stay in lucent, over these
operations, because they are what the route-level tests drive and a guard
that dissolves into the access layer is a guard nobody can see refuse.
"""

from __future__ import annotations

import datetime
import json
import time
from typing import Any, Mapping, Sequence

from . import _sql
from .core import _execute, _execute_returning, _fetch, _fetch_one
from .errors import MissingReference, StaleRead, ValueRefused
from .rows import UNSET, as_row, as_rows
from .schema import public as schema_public
from .schema import qualified, select_list
from .unit import Unit, transaction

#: The prune's own projection. Named here rather than built at the call site
#: so the nightly pass cannot ask for a column that is not there and then
#: report a clean run over the rows it failed to read.
PRUNE_COLUMNS = (
    "id", "content", "data_class", "tier", "mind_id", "source",
    "created_at", "codebase_ref", "expires_at",
)

_NODE_COLUMNS = schema_public("memory.nodes")
_EDGE_COLUMNS = schema_public("memory.edges")
_NODE_READ = select_list("memory.nodes")
_EDGE_READ = select_list("memory.edges")


def _now() -> float:
    """The epoch seconds a row's `created_at` takes when nobody passed one.

    These tables declare `created_at` NOT NULL with no server default — the
    SQLite originals filled it in Python — so a caller that simply omits it
    gets a row the database refuses. The clock lives here rather than in
    every call site, and it is overridable per call for the same reason
    every other boundary in this package is: a test must be able to say when
    something happened.
    """
    return time.time()


def _now_text() -> str:
    """The text `soul_changes.at` takes when nobody passed one.

    A text column rather than a timestamp, and NOT NULL: lucent writes an
    ISO string into it, and the record is read by people rather than joined
    on.
    """
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _blob(properties: Mapping[str, Any] | None) -> str:
    """Properties as the text the column actually holds.

    `memory.nodes.properties` is `text`, not `jsonb` — so this package does
    the encoding in one place rather than leaving every caller to agree on
    separators. Postgres would render a jsonb array as `["a", "b"]` where
    SQLite's `json_dumps` gives `["a","b"]`, and a `LIKE` pattern spanning a
    comma means different things against the two.
    """
    return json.dumps(properties or {}, separators=(",", ":"))


def _decoded(value: Any) -> dict[str, Any]:
    """A properties blob as a dict. An unreadable one is a refusal.

    Absent or empty is an empty blob, which is an ordinary node nobody has
    put properties on yet.

    Unreadable is not. This used to answer `{}` for a blob that would not
    parse, and the merge above it then wrote that `{}` plus its own
    additions back over the row — every other key on the node destroyed as
    a side effect of adding one, and the caller told the merge landed. A
    read-modify-write cannot modify what it could not read, so it refuses
    and says which row.
    """
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueRefused(
            f"this node's properties are not readable JSON, so a merge "
            f"would overwrite them: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ValueRefused(
            "this node's properties are not a JSON object, so a merge "
            f"would overwrite them: {type(decoded).__name__}")
    return decoded


def _node_key(type: str, name: str) -> str:
    """The advisory-lock key one node's writers serialize on.

    Keyed by the type and the name, so two writers of one node take turns
    and two writers of different nodes do not — the distinction requirement
    seven is about. The pair is rendered as JSON rather than joined on a
    separator: a name containing the separator must not be able to collide
    with a different pair. No NUL byte, either — the key crosses the wire as
    `text`, and Postgres refuses one outright.

    The owner is deliberately *not* in the key, even though the unique index
    is on the triple. The caller resolves which owner it is writing under by
    reading the row first, and that read is outside this lock — so a key
    including the owner is a key computed from state that can have moved. A
    Mind node waits ownerless until its credential is minted and the
    ownership pass then claims it, which is exactly a row whose owner
    changes under a concurrent writer: the in-transaction existence check
    below would miss it and insert a second node. Two people writing
    same-named nodes of the same type now take turns where they need not,
    which is a contention nobody will ever measure against a fork nobody
    would ever find.
    """
    return "memory.nodes:" + json.dumps([type, name])


async def node_write(*, owner: Any, type: str, name: str, mind_id: str,
                     properties: Mapping[str, Any] | None = None,
                     first_name: Any = UNSET,
                     last_name: Any = UNSET, data_class: Any = UNSET,
                     tier: Any = UNSET, source: Any = UNSET,
                     as_of: Any = UNSET, created_at: Any = UNSET,
                     updated_at: Any = UNSET) -> dict[str, Any]:
    """Write one node, replacing the whole properties blob, and return it.

    Full-replace is the contract and it is the half of the pair the operator
    is warned about: this route overwrites `properties` entirely, and
    `properties_merge` is the additive one. Keeping them different is a
    requirement, not an accident.

    Takes no `unit`. The write holds its own, because the lock and the
    find-then-write have to be in one transaction for either to mean
    anything, and a caller passing a unit that had already read something
    would get a hold that protects nothing — which `Unit.hold` refuses
    outright rather than allowing quietly.
    """
    values = _sql.passed({
        "mind_id": mind_id,
        "type": type,
        "name": name,
        "first_name": first_name,
        "last_name": last_name,
        "properties": _blob(properties),
        "data_class": data_class,
        "tier": tier,
        "source": source,
        "as_of": as_of,
        "created_at": _now() if created_at is UNSET else created_at,
        "updated_at": _now() if updated_at is UNSET else updated_at,
        "owner": owner,
    })
    async with transaction() as unit:
        await unit.hold(_node_key(type, name))
        existing = await unit.fetch_one(
            f'SELECT "id" FROM {qualified("memory.nodes")} '
            'WHERE "type" = $1 AND "name" = $2 '
            'AND "owner" IS NOT DISTINCT FROM $3 LIMIT 1',
            type, name, owner)
        if existing is None:
            sql, args = _sql.insert(
                "memory.nodes", values, returning=_NODE_COLUMNS)
            written = await unit.execute_returning(sql, *args)
            return dict(written["row"])
        updates = {k: v for k, v in values.items()
                   if k not in ("type", "name", "owner")}
        sql, args = _sql.update(
            "memory.nodes", updates, {"id": existing["id"]},
            returning=_NODE_COLUMNS)
        written = await unit.execute_returning(sql, *args)
        return dict(written["row"])


async def node_by_id(node_id: int, *,
                     unit: Unit | None = None) -> dict[str, Any] | None:
    """One node by its identity."""
    return await _fetch_one(
        unit, f'SELECT {_NODE_READ} FROM {qualified("memory.nodes")} '
        'WHERE "id" = $1', node_id)


async def node_find(*, type: str, name: str, owner: Any = UNSET,
                    unit: Unit | None = None) -> dict[str, Any] | None:
    """One node by type and name, optionally within one owner's graph.

    `owner` unset means every owner, which is the console and the hive's own
    services, and is also how a Mind node is found while it is still waiting
    for its credential to be minted. Passed, including passed as `None`, it
    is one owner — and `IS NOT DISTINCT FROM` rather than `=` because a NULL
    owner is a real case here and `= NULL` matches nothing.

    `ORDER BY "id"` is not decoration. The table is unique on *(owner, type,
    name)*, so two people may each hold a Mind node called Skippy, and an
    unordered `LIMIT 1` picks between them by whatever the planner felt
    like — which means an identity guard and the write that follows it can
    resolve to different rows, and the check then passes on one node while
    the write lands on another.
    """
    sql = (f'SELECT {_NODE_READ} FROM {qualified("memory.nodes")} '
           'WHERE "type" = $1 AND "name" = $2')
    args: list[Any] = [type, name]
    if owner is not UNSET:
        sql += ' AND "owner" IS NOT DISTINCT FROM $3'
        args.append(owner)
    return await _fetch_one(unit, sql + ' ORDER BY "id" LIMIT 1', *args)


async def nodes_for_owners(*, owners: Sequence[str] | None,
                           type: str | None = None,
                           limit: int = 500,
                           unit: Unit | None = None) -> list[dict[str, Any]]:
    """Every node in a set of owners' graphs.

    `None` means no restriction. An **empty** sequence is a recognised
    caller whose credential names no user, and it reaches nothing rather
    than everything — the fail-closed branch lucent writes as `AND 0`, which
    is not even valid SQL here, so a mechanical port turns the security
    branch into an error and the tempting repair turns it into a bypass.
    """
    sql = f'SELECT {_NODE_READ} FROM {qualified("memory.nodes")} WHERE TRUE'
    args: list[Any] = []
    if owners is not None:
        names = [str(o) for o in owners]
        if not names:
            return []
        sql += f' AND "owner" = ANY(${len(args) + 1}::text[])'
        args.append(names)
    if type is not None:
        sql += f' AND "type" = ${len(args) + 1}'
        args.append(type)
    sql += f' ORDER BY "id" LIMIT ${len(args) + 1}'
    args.append(limit)
    return await _fetch(unit, sql, *args)


async def node_delete(node_id: int, *,
                      unit: Unit | None = None) -> dict[str, Any]:
    """Remove one node and the edges hanging off it, together.

    One unit, because the edge delete and the node delete are two statements
    here where they were one implicit transaction in SQLite. Soul history is
    `ON DELETE RESTRICT`, so a node that has any refuses — and without the
    unit the edges would already be gone when it did, leaving a node with
    every relationship stripped and a caller told the delete failed.
    """
    async def _both(where: Unit) -> dict[str, Any]:
        edges = await where.execute(
            f'DELETE FROM {qualified("memory.edges")} '
            'WHERE "source_id" = $1 OR "target_id" = $1', node_id)
        node = await where.execute(
            f'DELETE FROM {qualified("memory.nodes")} WHERE "id" = $1',
            node_id)
        return {"rows": node["rows"], "edges_deleted": edges["rows"]}

    if unit is not None:
        return await _both(unit)
    async with transaction() as own:
        return await _both(own)


async def properties_merge(*, node_id: int,
                           additions: Mapping[str, Any],
                           columns: Mapping[str, Any] | None = None
                           ) -> dict[str, Any]:
    """Add keys to a node's properties without dropping the ones there.

    Additive is the contract, and additive has to mean additive with respect
    to *other callers* and not merely to itself. The row is locked for the
    length of the read and the write, so the second merge reads what the
    first one wrote instead of what was there before both started.

    `SELECT ... FOR UPDATE` rather than a hold on the triple: the node is
    already identified, and locking the row serializes only the writers of
    that node. Nobody waits on an unrelated mind's write.

    `columns` are the node's own columns the same call sets — `updated_at`,
    `as_of`, `first_name` and the rest. They are a parameter rather than a
    second call the caller makes afterwards, because a second call is a
    second transaction: the blob merge commits, the column write is refused,
    and the caller is handed a refusal for a merge that already landed.
    """
    async with transaction() as unit:
        row = await unit.fetch_one(
            f'SELECT "properties" FROM {qualified("memory.nodes")} '
            'WHERE "id" = $1 FOR UPDATE', node_id)
        if row is None:
            raise MissingReference(
                f"there is no node {node_id} to merge properties into. The "
                "merge is not an upsert: a node that is not there is a "
                "caller working from a stale id, and creating one here "
                "would hide that.")
        merged = _decoded(row["properties"])
        merged.update(dict(additions))
        values = _sql.passed({"properties": _blob(merged),
                              **dict(columns or {})})
        sql, args = _sql.update("memory.nodes", values, {"id": node_id},
                                returning=_NODE_COLUMNS)
        written = await unit.execute_returning(sql, *args)
        return dict(written["row"])


async def properties_remove(*, node_id: int,
                            keys: Sequence[str],
                            columns: Mapping[str, Any] | None = None
                            ) -> dict[str, Any]:
    """Drop named keys from a node's properties, under the same lock.

    `columns` are cleared in the same statement for the same reason the
    merge takes them: a caller that reported a column key as removed and
    then failed to clear it has reported something that is still there.
    """
    async with transaction() as unit:
        row = await unit.fetch_one(
            f'SELECT "properties" FROM {qualified("memory.nodes")} '
            'WHERE "id" = $1 FOR UPDATE', node_id)
        if row is None:
            raise MissingReference(
                f"there is no node {node_id} to remove properties from.")
        remaining = _decoded(row["properties"])
        removed = [k for k in keys if k in remaining]
        for key in removed:
            remaining.pop(key, None)
        values = _sql.passed({"properties": _blob(remaining),
                              **dict(columns or {})})
        sql, args = _sql.update("memory.nodes", values, {"id": node_id},
                                returning=_NODE_COLUMNS)
        written = await unit.execute_returning(sql, *args)
        return {"row": dict(written["row"]),
                "removed": removed + sorted(dict(columns or {}))}


async def edge_write(*, mind_id: str, source_id: int, target_id: int,
                     type: str, owner: Any = UNSET,
                     properties: Mapping[str, Any] | None = None,
                     as_of: Any = UNSET, source: Any = UNSET,
                     data_class: Any = UNSET, tier: Any = UNSET,
                     created_at: Any = UNSET,
                     unit: Unit | None = None) -> dict[str, Any]:
    """Write one edge, naming the absent parent when one is absent.

    `memory.edges.mind_id` references `core.minds` and `memory.nodes.mind_id`
    does not, which makes an upsert's two halves fail differently: the node
    lands and the edge does not. The caller is told which parent is missing,
    by value, because the database's own sentence names a constraint and the
    person reading the log needs the mind.
    """
    values = _sql.passed({
        "mind_id": mind_id,
        "source_id": source_id,
        "target_id": target_id,
        "type": type,
        "properties": _blob(properties),
        "as_of": as_of,
        "source": source,
        "data_class": data_class,
        "tier": tier,
        "created_at": _now() if created_at is UNSET else created_at,
        "owner": owner,
    })
    sql, args = _sql.insert("memory.edges", values, returning=_EDGE_COLUMNS)
    try:
        written = await _execute_returning(unit, sql, *args)
    except MissingReference as absent:
        raise MissingReference(
            f"the edge was refused because a row it names is not there: "
            f"mind_id {mind_id!r}, source node {source_id}, target node "
            f"{target_id}. A mind writes edges only once it is registered "
            f"with the gateway, and `memory.edges.mind_id` is a real "
            f"foreign key into `core.minds` where the node table has none. "
            f"({absent})") from absent
    return dict(written["row"])


async def edges_for_nodes(*, node_ids: Sequence[int],
                          unit: Unit | None = None) -> list[dict[str, Any]]:
    """Every edge touching any of these nodes."""
    ids = [int(n) for n in node_ids]
    if not ids:
        return []
    return await _fetch(
        unit, f'SELECT {_EDGE_READ} FROM {qualified("memory.edges")} '
        'WHERE "source_id" = ANY($1::bigint[]) '
        'OR "target_id" = ANY($1::bigint[]) ORDER BY "id"', ids)


async def edge_delete(*, source_id: int, target_id: int, type: str,
                      unit: Unit | None = None) -> dict[str, Any]:
    """Remove one relationship between two nodes."""
    return await _execute(
        unit, f'DELETE FROM {qualified("memory.edges")} '
        'WHERE "source_id" = $1 AND "target_id" = $2 AND "type" = $3',
        source_id, target_id, type)


async def soul_change_record(*, node_id: int, mind_name: str,
                             mind_id: Any, actor: Any, reason: Any,
                             before: Any, after: Any,
                             at: Any = UNSET, created_at: Any = UNSET,
                             unit: Unit | None = None) -> dict[str, Any]:
    """Record one change to a soul against the node it changed."""
    values = _sql.passed({
        "node_id": node_id,
        "mind_name": mind_name,
        "mind_id": mind_id,
        "actor": actor,
        "reason": reason,
        "before_json": json.dumps(before if before is not None else {}),
        "after_json": json.dumps(after if after is not None else {}),
        "at": _now_text() if at is UNSET else at,
        "created_at": _now() if created_at is UNSET else created_at,
    })
    sql, args = _sql.insert("memory.soul_changes", values, returning=("id",))
    written = await _execute_returning(unit, sql, *args)
    return dict(written["row"])


async def soul_replace(*, node_id: int, properties: Mapping[str, Any],
                       record_against: int | None = None,
                       mind_name: str, mind_id: Any, actor: Any,
                       reason: Any, before: Any, after: Any,
                       at: Any = UNSET,
                       as_of: Any = UNSET,
                       updated_at: Any = UNSET,
                       expected_change_count: int | None = None
                       ) -> dict[str, Any]:
    """Replace a soul and record what it was, or do neither.

    `at` is the moment the caller stamped the change with, and it reaches
    `soul_changes.at`. Defaulted here it would be this write's own clock,
    which is a different thing: the route reports back the stamp it was
    given, and a record carrying a stamp nobody sent is a small lie in the
    one table that exists to say what happened and when.

    `as_of` and `updated_at` move on the node in the *same statement* as the
    properties. The SQLite original set all three together, and splitting
    them into a second call to keep this signature smaller would reintroduce
    exactly the non-atomicity this function exists to prevent.

    `expected_change_count` is the compare-and-set, and it is checked *here*
    rather than by the caller before calling. The caller reading the count
    and then calling this was two transactions with nothing held between
    them: two console saves both read the same count, both found it
    matching, and both wrote — the second over the first, and both told the
    operator their line landed. That is the one loss this whole pair exists
    to prevent, so the check cannot live outside the unit that writes.

    The node row is locked first. Counting `soul_changes` does not block
    another session's insert into it, so the count read needs something
    that serializes the writers — and every writer comes through here and
    takes the same row lock before it counts.

    `record_against` exists for the test that proves the pair is atomic: the
    provenance row is written against that id rather than the node's own, so
    a deliberately absent one makes the second statement fail and the first
    has to roll back with it. In every real call it is left unset.
    """
    against = node_id if record_against is None else record_against
    node_values = _sql.passed({
        "properties": _blob(properties),
        "as_of": as_of,
        "updated_at": updated_at,
    })
    sql, args = _sql.update("memory.nodes", node_values, {"id": node_id})
    async with transaction() as unit:
        await unit.execute(
            f'SELECT "id" FROM {qualified("memory.nodes")} '
            'WHERE "id" = $1 FOR UPDATE', node_id)
        count = await soul_change_count(node_id=node_id, unit=unit)
        if (expected_change_count is not None
                and expected_change_count != count):
            raise StaleRead(
                f"soul {node_id} has {count} changes, not "
                f"{expected_change_count}",
                expected=expected_change_count, observed=count)
        await unit.execute(sql, *args)
        await soul_change_record(
            node_id=against, mind_name=mind_name, mind_id=mind_id,
            actor=actor, reason=reason, before=before, after=after,
            at=at, unit=unit)
        return {"rows": 1, "node_id": node_id, "change_count": count + 1}


async def soul_history(*, node_id: int, limit: int = 50,
                       unit: Unit | None = None) -> list[dict[str, Any]]:
    """A soul's recorded changes, newest first.

    Ordered by the identity rather than the timestamp. Two changes written
    in the same second carry the same `at`, and a tie makes the order
    arbitrary — which reads as a record that reshuffles itself between two
    views of the same page.
    """
    return await _fetch(
        unit, f'SELECT {select_list("memory.soul_changes")} '
        f'FROM {qualified("memory.soul_changes")} '
        'WHERE "node_id" = $1 ORDER BY "id" DESC LIMIT $2',
        node_id, limit)


async def soul_change_count(*, node_id: int,
                            unit: Unit | None = None) -> int:
    """How many changes a soul has.

    Raises rather than answering zero when the read fails. The count is one
    side of a compare-and-set: a zero nobody measured makes the check pass
    unconditionally, and two console saves then overwrite each other with
    both told they succeeded.
    """
    row = await _fetch_one(
        unit, f'SELECT COUNT(*) AS "changes" '
        f'FROM {qualified("memory.soul_changes")} WHERE "node_id" = $1',
        node_id)
    return 0 if row is None else int(row["changes"])


async def type_put(*, name: str, kind: str,
                   property_schema: Mapping[str, Any] | None = None,
                   created_at: Any = UNSET,
                   unit: Unit | None = None) -> dict[str, Any]:
    """Register one node type, or leave the registered one as it is.

    `memory.nodes.type` is a real foreign key into this table, so seeding it
    is not optional on any database that is not the final load: without it
    every write is refused and the refusal reads `Must be one of: []`.
    """
    values = _sql.passed({
        "name": name,
        "kind": kind,
        "property_schema": json.dumps(dict(property_schema or {})),
        "created_at": _now() if created_at is UNSET else created_at,
    })
    sql, args = _sql.insert("memory.schema_types", values)
    sql += ' ON CONFLICT ("name") DO NOTHING'
    return await _execute(unit, sql, *args)


async def type_spec(*, name: str,
                    unit: Unit | None = None) -> dict[str, Any] | None:
    """One registered type."""
    return await _fetch_one(
        unit, f'SELECT {select_list("memory.schema_types")} '
        f'FROM {qualified("memory.schema_types")} WHERE "name" = $1', name)


async def types_all(*, unit: Unit | None = None) -> list[dict[str, Any]]:
    """Every registered node type, by name."""
    return await _fetch(
        unit, f'SELECT {select_list("memory.schema_types")} '
        f'FROM {qualified("memory.schema_types")} ORDER BY "name"')


async def edge_type_put(*, name: str, source_type: str, target_type: str,
                        attr_schema: Mapping[str, Any] | None = None,
                        symmetric: Any = UNSET, created_at: Any = UNSET,
                        unit: Unit | None = None) -> dict[str, Any]:
    """Register one relationship, or leave the registered one as it is."""
    values = _sql.passed({
        "name": name,
        "source_type": source_type,
        "target_type": target_type,
        "attr_schema": json.dumps(dict(attr_schema or {})),
        "symmetric": symmetric,
        "created_at": _now() if created_at is UNSET else created_at,
    })
    sql, args = _sql.insert("memory.schema_edges", values)
    sql += ' ON CONFLICT ("name") DO NOTHING'
    return await _execute(unit, sql, *args)


async def edge_type_spec(*, name: str,
                         unit: Unit | None = None) -> dict[str, Any] | None:
    """One registered relationship."""
    return await _fetch_one(
        unit, f'SELECT {select_list("memory.schema_edges")} '
        f'FROM {qualified("memory.schema_edges")} WHERE "name" = $1', name)


async def edge_types_all(*, unit: Unit | None = None) -> list[dict[str, Any]]:
    """Every registered relationship, by name."""
    return await _fetch(
        unit, f'SELECT {select_list("memory.schema_edges")} '
        f'FROM {qualified("memory.schema_edges")} ORDER BY "name"')


async def credential_put(*, holder: str, username: Any, role: str,
                         token_sha: str, mind_id: Any = None,
                         created_at: Any = UNSET,
                         unit: Unit | None = None) -> dict[str, Any]:
    """Store or replace one holder's credential.

    Replacing clears a revocation, which is the behaviour the console's
    re-mint depends on: a holder whose value was revoked and then minted
    again is a live holder, not a revoked one with a new digest.
    """
    values = _sql.passed({
        "holder": holder,
        "username": username,
        "role": role,
        "mind_id": mind_id,
        "token_sha": token_sha,
        "created_at": _now() if created_at is UNSET else created_at,
    })
    sql, args = _sql.insert("memory.credentials", values)
    sql += (' ON CONFLICT ("holder") DO UPDATE SET '
            '"username" = EXCLUDED."username", "role" = EXCLUDED."role", '
            '"mind_id" = EXCLUDED."mind_id", '
            '"token_sha" = EXCLUDED."token_sha", "revoked_at" = NULL')
    return await _execute(unit, sql, *args)


async def credential_revoke(*, holder: str, at: Any = UNSET,
                            unit: Unit | None = None) -> dict[str, Any]:
    """Revoke one holder's credential, touching only that holder.

    `revoked_at` is epoch seconds rather than a timestamp — every time
    column these tables carry is, because they were SQLite columns filled by
    Python — so the moment is passed in as a number and `NOW()` would be
    refused outright.
    """
    return await _execute(
        unit, f'UPDATE {qualified("memory.credentials")} '
        'SET "revoked_at" = $2 '
        'WHERE "holder" = $1 AND "revoked_at" IS NULL',
        holder, _now() if at is UNSET else at)


async def credential_by_token(*, token_sha: str,
                              unit: Unit | None = None
                              ) -> dict[str, Any] | None:
    """The live holder a digest belongs to, or None.

    Revoked rows are not returned. A revoked credential resolving to its
    holder and being refused one layer up is one more place the refusal can
    be forgotten.
    """
    return await _fetch_one(
        unit, f'SELECT {select_list("memory.credentials")} '
        f'FROM {qualified("memory.credentials")} '
        'WHERE "token_sha" = $1 AND "revoked_at" IS NULL', token_sha)


async def credentials_all(*, unit: Unit | None = None) -> list[dict[str, Any]]:
    """Every holder, revoked or not, for the console's own page."""
    return await _fetch(
        unit, f'SELECT {select_list("memory.credentials")} '
        f'FROM {qualified("memory.credentials")} ORDER BY "holder"')


async def credentials_configured(*, unit: Unit | None = None) -> bool:
    """Whether any live credential exists.

    Its own operation rather than a length check on `credentials_all`,
    because the caller refuses to serve at all when this is false: a read
    that failed and was caught would answer "no credentials" about a store
    whose credentials are fine, and send the operator to the wrong problem.
    """
    row = await _fetch_one(
        unit, f'SELECT 1 AS "any" FROM {qualified("memory.credentials")} '
        'WHERE "revoked_at" IS NULL LIMIT 1')
    return row is not None


async def memories_for_class(*, data_class: str,
                             include_standing: bool = False,
                             unit: Unit | None = None
                             ) -> list[dict[str, Any]]:
    """The nightly prune's read, against the store the deletes land in.

    The prune used to open the SQLite file directly, on a path that was not
    even the one lucent itself used, and delete over HTTP. Once the memories
    are here that read finds nothing and the pass reports a clean run having
    deleted none — or, with the old file still in place, it reads ids the
    load renumbered and deletes rows it never looked at.
    """
    columns = ", ".join(f'"{c}"' for c in PRUNE_COLUMNS)
    sql = (f'SELECT {columns} FROM {qualified("memories")} '
           'WHERE "data_class" = $1')
    args: list[Any] = [data_class]
    if not include_standing:
        sql += ' AND "tier" IS DISTINCT FROM \'standing\''
    return await _fetch(unit, sql + ' ORDER BY "id"', *args)


#: The two graph tables that carry an owner, and whether they carry a type.
#:
#: `edges` has no `type` column in the sense the ownership pass means — its
#: `type` is the relationship, not the entity kind — so the pass must not
#: read one off it and conclude the row is a Mind.
_OWNED_TABLES = (("memory.nodes", True), ("memory.edges", False))


async def unowned_rows(*, limit: int = 5000,
                       unit: Unit | None = None) -> list[dict[str, Any]]:
    """Every graph row with no owner, with the writer that can resolve one.

    The ownership pass is self-healing by design: a mind whose credential
    was minted after the last boot is picked up at the next one. So this is
    a read that runs at every start and is expected to be empty almost
    always, and it names its table in each row so the caller can claim it
    without a second lookup.
    """
    out: list[dict[str, Any]] = []
    for table, has_type in _OWNED_TABLES:
        type_column = '"type"' if has_type else "NULL"
        rows = await _fetch(
            unit, f'SELECT "id", "mind_id", {type_column} AS "type" '
            f'FROM {qualified(table)} WHERE "owner" IS NULL '
            f'ORDER BY "id" LIMIT $1', limit)
        for row in rows:
            out.append({**row, "table": table})
    return out


async def claim_owner(*, table: str, row_id: int, owner: str,
                      unit: Unit | None = None) -> dict[str, Any]:
    """Give one ownerless row an owner.

    Only ownerless rows: the `WHERE owner IS NULL` is the arbitration. Two
    passes running at once — two containers starting together — would
    otherwise be two writers deciding the same row, and the second would
    overwrite a claim the first had already made.
    """
    if table not in dict(_OWNED_TABLES):
        raise ValueRefused(
            f"{table!r} is not a table the ownership pass may write. The "
            f"name reaches a statement, so it is checked against the "
            f"declared set rather than interpolated: "
            f"{', '.join(t for t, _ in _OWNED_TABLES)}.")
    return await _execute(
        unit, f'UPDATE {qualified(table)} SET "owner" = $1 '
        'WHERE "id" = $2 AND "owner" IS NULL', owner, row_id)


async def credentials_live(*, unit: Unit | None = None) -> list[dict[str, Any]]:
    """Every credential that has not been revoked.

    A read of all of them rather than a lookup by digest, because the caller
    compares in constant time over the set: an indexed equality on
    `token_sha` would decide recognition inside the database, where the
    comparison cannot be made constant-time and where two holders sharing
    one value would resolve to whichever row the planner reached first —
    which is one holder authenticating as another.
    """
    return await _fetch(
        unit, f'SELECT {select_list("memory.credentials")} '
        f'FROM {qualified("memory.credentials")} '
        'WHERE "revoked_at" IS NULL ORDER BY "holder"')


async def credential_holder_of_digest(*, token_sha: str, other_than: str,
                                      unit: Unit | None = None
                                      ) -> str | None:
    """Which other holder already has this digest, if any.

    The clash check, kept as its own operation because its refusal names the
    holder: "that value is already held by X; a credential belongs to one
    holder" is the sentence an operator needs, and a unique-constraint
    violation is not it.
    """
    row = await _fetch_one(
        unit, f'SELECT "holder" FROM {qualified("memory.credentials")} '
        'WHERE "token_sha" = $1 AND "holder" <> $2', token_sha, other_than)
    return None if row is None else str(row["holder"])
