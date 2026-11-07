"""Lucent's store: the hive's one database, through the access layer.

This module used to own a SQLite file — a per-thread connection, the schema
that created itself on first use, and the migrations that brought an older
file forward. All of that is gone. The schema is applied to Postgres as an
artifact, `hive_data` owns the connections, and what is left here is the two
things that are genuinely lucent's and not the database's:

* the store's lifecycle, opened once from the server's lifespan and closed
  once at shutdown, because nothing in `hive_data` opens a pool on demand and
  a lazy open under two concurrent first calls builds two pools; and
* the ownership pass, which is self-healing on purpose and so cannot simply
  die with the schema init it used to hang off. A `Mind` node whose
  credential had not been minted yet was deliberately left ownerless to wait
  for "a later pass" — and if there is no later pass, the boys' minds stay
  ownerless for good, each able to find its own node and nothing else in its
  graph, with no error anywhere saying so.

There is no synchronous face. `hive_data` has none by design, and a bridge
written here would be the one place in the hive that blocks an event loop on
a database call.
"""

from __future__ import annotations

import logging

import hive_data
from hive_data import graph

log = logging.getLogger("lucent-api")


async def open_store() -> None:
    """Open the pool and check the schema, once, from the lifespan.

    The schema check is `hive_data`'s and it is left on: a database that is
    not the one these operations were written against is a process that
    refuses to start, rather than one that answers every memory write with a
    missing key.

    Idempotent, because this process has two entry points — the
    nervous-system container runs the app beside a scheduler, and
    `python -m lucent_api.server` runs it alone — and opening a second pool
    would hold double the connections against a ceiling shared with every
    other service, with nothing reporting that it happened.
    """
    try:
        hive_data.live_pool()
    except hive_data.NotInitialised:
        await hive_data.connect()
        return
    log.info("lucent.store.open: a pool is already open; leaving it alone")


async def close_store() -> None:
    """Close the pool, letting work in flight finish."""
    await hive_data.close(drain_timeout=5.0)


async def store_is_live() -> bool:
    """Whether the store answers right now.

    Asked by `/health`, which used to be a hardcoded dict. That was
    defensible against a local file that essentially never failed; against a
    database over a socket it makes liveness unobservable, so a restarted
    Postgres leaves every healthcheck green while every write returns an
    error body.
    """
    try:
        await hive_data.run(lambda connection: connection.fetchval("SELECT 1"))
    except Exception:
        log.exception("lucent.store.health: the store did not answer")
        return False
    return True


async def owners_by_writer() -> dict[str, str]:
    """Each mind's owner, as lucent's own credential table records it.

    Lucent's credentials carry the user their holder answers for, which is
    the only owner fact this process holds. Comms' `minds.owner` is the same
    fact recorded by another service, and reaching into it behind comms'
    back would make two readers of one truth.
    """
    owners: dict[str, str] = {}
    for row in await graph.credentials_all():
        if row.get("revoked_at") is not None:
            continue
        user = str(row.get("username") or "").strip().lower()
        if not user:
            continue
        for key in (str(row.get("mind_id") or "").strip(),
                    str(row.get("holder") or "").strip().lower()):
            if key:
                owners[key] = user
    return owners


async def backfill_owners() -> dict[str, int]:
    """Give every ownerless graph row the owner of the mind that wrote it.

    Run at every start, and idempotent, because it is the mechanism by which
    a mind that was not yet known to this store becomes known: a credential
    minted after the last boot is picked up at the next one.

    A `Mind` node whose writer cannot be resolved stays ownerless rather than
    going to the primary user. Handing it over would take one of the boys'
    minds' souls away from them and give it to the operator, after which the
    owner check refuses that mind sight of its own node. Everything else
    unresolved goes to the primary user when one is named, and stays
    ownerless when none is: a node only a service can reach is a smaller
    harm than a node belonging to the wrong person.

    Reports counts rather than logging and returning nothing, so the caller
    can say what the pass did and a pass that claimed nothing is
    distinguishable from one that never ran.
    """
    from lucent_api.holder_access import primary_user

    owners = await owners_by_writer()
    primary = (primary_user() or "").strip().lower() or None
    claimed = 0
    collided = 0
    left = 0

    def owner_for(mind_id: object, node_type: object) -> str | None:
        key = str(mind_id or "").strip()
        resolved = owners.get(key) or owners.get(key.lower())
        if resolved:
            return resolved
        if str(node_type or "") == "Mind":
            return None
        return primary

    for row in await graph.unowned_rows():
        resolved = owner_for(row.get("mind_id"), row.get("type"))
        if not resolved:
            left += 1
            continue
        try:
            await graph.claim_owner(
                table=row["table"], row_id=row["id"], owner=resolved)
            claimed += 1
        except hive_data.Conflict:
            # Claiming this row would collide with one the owner already
            # holds under the same type and name. Left ownerless and
            # counted: one row only a service can reach is a smaller harm
            # than a start-up pass that raises and takes the store with it.
            collided += 1
            log.error(
                "graph.owner.backfill.skipped: %s row %s would collide "
                "under owner %s", row["table"], row["id"], resolved)
    if claimed or collided or left:
        log.info(
            "graph.owner.backfill: %d claimed, %d collided, %d left "
            "ownerless", claimed, collided, left)
    return {"claimed": claimed, "collided": collided, "ownerless": left}
