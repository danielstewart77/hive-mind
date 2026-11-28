"""Which mind is talking to the memory store, decided by the value it presented.

Lucent began by trusting the caller. `mind_id` arrived in the request body —
provenance on a write, an opt-in filter on a read — behind one shared
`LUCENT_BEARER_TOKEN` that every mind, every hook and every background process
on every host already held. So any holder of that value could file a memory
under any mind's name and read every mind's memories, and the default on
`/memory/store` was `mind_id="ada"`, which is how an unattributed write got
somebody's name on it.

## The credential names the holder, and the holder is the mind

A **holder** is one box: a mind, a service, or a person. Each holds its own
opaque value, stored here as a SHA-256 digest and held in plaintext by nothing
but the holder. For a holder whose role is `mind`, the holder *is* the mind —
so which mind a memory is filed under is read off the credential and the body
cannot say otherwise.

Keying on the mind rather than on its owner is deliberate and is not the same
choice comms makes. Comms decides *whose conversation this is*, which is a
question about a person; ten of this hive's twelve minds belong to one person,
so answering provenance with the owner would file ten minds' memories under
one name and leave every per-mind read returning the union or nothing. A
conversation answers to a person. A memory answers to the mind that formed it.

## The graph answers to a person, and that is not an inconsistency

A node is not a memory. A memory is one mind's recollection, formed in one
mind's conversation, and nothing else should be able to recall it as its own.
The graph is the facts a person's minds hold in common — who their people are,
what their machines are, what their projects are — and a fact learned by one
of a person's minds is useless locked inside that mind. So nodes are keyed by
`owner`, every one of that person's minds reads and writes all of it, and
nothing crosses between people.

The single exception is a `Mind` node, which holds that mind's soul. Within
one owner's graph it is the one row the owner's other minds may read and not
change, and `mind_id` is still what decides that: a `Mind` node's provenance
is the mind it represents.

## A service reads on behalf of a mind, and says so

Comms composes every mind's system prompt — soul from the graph, decay-weighted
recent memory — which means one service legitimately reads many minds' records.
That is a third shape, neither "trust the body" nor "only your own": a holder
whose role is `service`, and an admin, may name the mind it is acting for, and
nothing else may. A `mind` holder naming any mind but itself is refused rather
than quietly rescoped, because a mind that asks for somebody else's memory has
either been misconfigured or been taken.

## The `shared` sentinel is not a mind and is kept on purpose

Hive-wide standing rules are written with `mind_id` set to the literal
`shared`, and every mind's contextual retrieval reads its own plus that one.
It has no credential and no owner, so deriving provenance strictly from the
holder would convert every hive-wide rule into a private one and make the
seven already on disk unreadable to everybody. A `mind` holder may therefore
write `shared` explicitly, and every holder reads it alongside its own.

## There is no bypass

An unset token used to mean "serve everything to everybody", as a convenience
for a container that had not been configured yet. A store that cannot
establish who is calling refuses: 503, naming the reason, rather than handing
every mind's memory to the LAN while it waits for an operator to notice.

There is no administrator credential. A key is recognised from the one record
in `core.credentials`, read on every request (`hive_data.access`): recorded,
replaced or cut off once there, it takes effect here on the next request. This
store registers no keys of its own. What an administrator does here is done
with the administrator's own person key (`auth.require_admin_bearer`).

The *variable* holders present is still `LUCENT_BEARER_TOKEN`, deliberately.
It is the same slot it always was, and renaming it would mean editing every
hook, skill and service on every host to say the same thing differently. What
changed is underneath: the store no longer holds a copy to compare against, so
each host's variable carries that holder's own value and is checked against the
table. The rollout is therefore a different value written into a variable that
already exists, and a holder that was missed is refused rather than quietly
sharing somebody else's identity.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from fastapi import Header, HTTPException, Request

from hive_data import access, core

#: What a holder is. A mind or a service is the machinery; a person is not.
MIND = "mind"
SERVICE = "service"
PERSON = "person"
ROLES = (MIND, SERVICE, PERSON)

#: Not a mind. The provenance hive-wide standing rules are written under, read
#: by every mind alongside its own.
SHARED = "shared"

#: Who a graph row belongs to when the credential that wrote it answers for
#: nobody — the admin value, which is the house key rather than a person.
#: Named by the environment: a person's name in shipped code is one install's
#: code. Shared with `lucent._migrate_graph_owner`, which uses it for the
#: same decision about rows already on disk.
PRIMARY_USER_ENV = "LUCENT_PRIMARY_USER"


def primary_user() -> str:
    return str(os.getenv(PRIMARY_USER_ENV, "")).strip().lower()

@dataclass(frozen=True)
class Caller:
    """Who the store decided is asking."""

    holder: str = ""
    username: str = ""
    role: str = ""
    #: The provenance this holder's rows carry, when it differs from the
    #: holder string. A mind minted as "skippy" whose rows are keyed by uuid
    #: names that uuid here, and both spellings are accepted from the caller.
    mind_id: str = ""
    is_admin: bool = False

    @property
    def recognised(self) -> bool:
        return bool(self.holder) or self.is_admin

    @property
    def provenance(self) -> str:
        """What this holder's rows are filed under."""
        return self.mind_id or self.holder

    @property
    def own_names(self) -> set[str]:
        """Every spelling of itself this holder may use."""
        return {n for n in (self.holder, self.mind_id) if n}

    @property
    def may_act_for_another_mind(self) -> bool:
        """An admin, or a service acting on a mind's behalf.

        Comms composing a prompt is the whole of this: it reads each mind's
        soul and recent memory to build the dispatch. A `mind` holder is
        excluded on purpose — a mind asking for another mind's record is
        either misconfigured or compromised, and there is no third reading.
        """
        return self.is_admin or self.role == SERVICE

    def mind_for_write(self, named: object = None, required: bool = True) -> str:
        """Which mind a write is filed under.

        For a mind, itself — the body cannot say otherwise, except to name the
        `shared` sentinel explicitly. For a service or an admin, the mind it
        named, because it has no mind of its own to file under.
        """
        asked = str(named or "").strip()
        if self.role == MIND:
            if asked and asked.lower() == SHARED:
                return SHARED
            if asked and asked not in self.own_names and (
                asked.lower() not in self.own_names
            ):
                raise HTTPException(
                    403,
                    "a mind files memories under itself; "
                    f"{self.holder!r} may not write as {asked!r}",
                )
            return self.provenance
        if not self.may_act_for_another_mind:
            raise HTTPException(
                403, "this credential has no mind to file a memory under"
            )
        if not asked:
            if not required:
                # A graph write whose body carries no provenance at all, which
                # is how the console has always created edges. Refusing it
                # would answer an edge-creation call with a sentence about
                # memories and services.
                return ""
            raise HTTPException(
                400, "a service writing on a mind's behalf must name the mind"
            )
        return asked

    @property
    def owner(self) -> str:
        """Whose graph this caller writes into and reads from.

        The credential row names the user its holder answers for, and that
        user is the graph's partition. This is deliberately a different
        answer from `provenance`, which stays per-mind: a memory answers to
        the mind that formed it, while the graph is one space per person
        that all of that person's minds build together.
        """
        return normalize_holder(self.username)

    def owners_for_read(self) -> list[str] | None:
        """Which owners' nodes this caller may see, or None for all of them.

        A mind sees its owner's graph entire, whichever of that owner's
        minds wrote each row. A service and an admin see everything: comms
        composes every mind's prompt and the console draws every mind's
        page, and neither has a person of its own to be scoped to.
        """
        if self.may_act_for_another_mind:
            return None
        if not self.owner:
            # A recognised holder whose row names no user. It reaches no
            # owner's graph rather than everyone's.
            return []
        return [self.owner]

    def owner_for_write(self, named: object = None) -> str | None:
        """Which owner's graph a write may *reach*, or None for every one.

        Two different questions hide here and conflating them cost the
        console and the operator their write access: which rows a call may
        reach, and what owner a brand-new row is stamped with. This is the
        first. A mind reaches its own owner's graph and the body cannot say
        otherwise. A service and an admin reach every owner's, exactly as
        they read every owner's — the console edits whichever mind's page the
        operator opened, and the admin credential is how a wedged record gets
        fixed. Naming an owner narrows them to it.
        """
        asked = normalize_holder(named)
        if self.may_act_for_another_mind:
            return asked or None
        if asked and asked != self.owner:
            raise HTTPException(
                403,
                f"{self.holder!r} writes into {self.owner!r}'s graph, not {asked!r}'s",
            )
        if not self.owner:
            raise HTTPException(
                403, "this credential has no owner whose graph it could write to"
            )
        return self.owner

    def owner_to_stamp(self, named: object = None) -> str:
        """The owner a row this call *creates* is filed under.

        Unlike the scope, this cannot be "every owner": a row has one. A
        mind stamps its own owner. A service stamps the owner it named, or
        the user its own credential answers for. The admin credential
        answers for nobody — it is the house key, not a person — so it
        stamps the primary user, and where none is declared it is refused
        rather than writing a row under the empty string, which is a node
        no mind can ever read.
        """
        asked = normalize_holder(named)
        if asked and not self.may_act_for_another_mind and asked != self.owner:
            raise HTTPException(
                403,
                f"{self.holder!r} writes into {self.owner!r}'s graph, not {asked!r}'s",
            )
        stamped = asked or self.owner or primary_user()
        if not stamped:
            raise HTTPException(
                403,
                "this credential names no owner to file a new node under; set "
                f"{PRIMARY_USER_ENV} or name an owner on the request",
            )
        return stamped

    def minds_for_read(self, named: object = None) -> list[str] | None:
        """Which minds a read may see, or None for every one of them.

        A mind sees itself and the `shared` sentinel. A service or an admin
        sees the mind it named, or everything when it names none — which is
        what the console's own pages and the nightly pruner need.
        """
        asked = str(named or "").strip()
        if self.role == MIND:
            if asked and asked.lower() == SHARED:
                return [SHARED]
            if asked and asked not in self.own_names and (
                asked.lower() not in self.own_names
            ):
                raise HTTPException(
                    403,
                    f"{self.holder!r} may not read {asked!r}'s memory",
                )
            return [self.provenance, SHARED]
        if not self.may_act_for_another_mind:
            raise HTTPException(403, "this credential reaches no mind's memory")
        return [asked] if asked else None


def digest(token: str) -> str:
    """The stored form of a key, as the one record keeps it."""
    return access.digest(token)


def normalize_holder(name: object) -> str:
    return str(name or "").strip().lower()


async def credential_for_token(token: str) -> Caller | None:
    """The holder a presented key belongs to, or None.

    Read from the one record (`core.credentials`) per call, never cached: a
    key replaced or cut off there is refused here on the next request. A
    person's own key is no memory-store caller (it is accepted only for the
    administrator and soul routes, which read it themselves).
    """
    key = await access.key_for(token, service="memory")
    if key is None or key.role == access.USER_KEY:
        return None
    role = key.role if key.role in ROLES else SERVICE
    mind_id = ""
    if role == MIND:
        known = await core.get_mind_by_id_or_name(key.holder)
        if known is not None:
            mind_id = str(known["mind_id"] or "")
    return Caller(holder=key.holder, username=key.username, role=role,
                  mind_id=mind_id)


def _bearer(authorization: str) -> str:
    """The presented value, decoded the same way on every path.

    Decoded as the header arrived rather than re-encoded: a value pasted onto
    a Windows box through a document picks up a non-breaking hyphen or a smart
    quote without showing it, and digesting the two spellings differently
    refuses that holder forever with the same message a wrong value gives.
    """
    if not authorization.startswith("Bearer "):
        return ""
    return authorization[7:].strip()


async def resolve_caller(token: str) -> Caller:
    found = await credential_for_token(token)
    return found or Caller()


#: The one read a signed-in person's own key may make of the graph: the picture
#: the hive site draws for them. Nothing else of the graph or of memory.
PERSON_GRAPH_READ = ("GET", "/graph/data")


async def _person_graph_caller(token: str) -> Caller | None:
    """The caller for a person's own key on the graph picture: their own graph,
    or every graph for an administrator. None for any other key."""
    key = await access.key_for(token)
    if key is None or key.role != access.USER_KEY:
        return None
    if await access.live_person(key.username) is None:
        return None
    return Caller(holder=key.username, username=key.username, role=PERSON,
                  is_admin=await access.person_is_administrator(key.username))


async def _store_configured() -> bool:
    """Whether the one record holds any live key at all.

    The read is not wrapped in a broad `except`. A failure here means the
    database did not answer, and reporting that as "no credentials
    configured" sends the operator to the credentials page over a store
    whose credentials are fine.
    """
    return bool(await core.list_live_credentials())


async def require_holder(authorization: str = Header(default=""),
                         request: Request = None) -> Caller:  # type: ignore[assignment]
    """The FastAPI dependency every memory and graph route is gated by.

    Returns the caller rather than None, because which mind is asking is the
    answer the routes need and reading it off the body is the thing this
    replaced.
    """
    if not await _store_configured():
        # Not a 401: nothing the caller presented was wrong. The store cannot
        # establish who anybody is, and serving open while it waits is how
        # every mind's memory ends up readable from the LAN.
        raise HTTPException(
            503,
            "Memory store has no credentials configured and will not serve open",
        )
    token = _bearer(authorization)
    if not token:
        raise HTTPException(401, "Missing or invalid Authorization header")
    caller = await resolve_caller(token)
    if (not caller.recognised and request is not None
            and (request.method, request.url.path) == PERSON_GRAPH_READ):
        caller = await _person_graph_caller(token) or caller
    if not caller.recognised:
        raise HTTPException(401, "Invalid token")
    return caller
