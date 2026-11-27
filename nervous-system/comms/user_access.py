"""Who is asking, and which conversations are theirs.

Comms began with no notion of a person. Its credentials answered *what* was
calling and never *who for*, so every session route was open to every caller
holding any of them, and a second person on the hive would see the first
person's conversations.

A conversation's owner is **derived, never stored**: a session names a mind,
and `broker.minds` names that mind's owner. There is no owner column on
`sessions`, because a second place to record the same fact is a second place
for it to be wrong, and a mind handed to another user would leave every one
of its past conversations claiming the old one.

## The credential belongs to a holder; the authority belongs to its owner

A **holder** is one box: a mind, a service, or a person. Each holds its own
opaque random value, stored here as a SHA-256 digest in `core.credentials` and held
in plaintext by nothing but the holder itself. The row names the user that
holder answers for, and *that* is what every ownership decision is made
against — the holder is which machine is talking, the user is whose things it
may touch.

Those are two different facts and only one of them gets stolen. One value per
*person* would mean the credential sitting on a Windows box in a child's
bedroom was that child's entire identity: lift it and you are them across
every mind they own, their board, their memory and their integrations, and
the only remedy is revoking the person and re-credentialing every machine
they have, including the ones nobody can reach. One value per holder makes a
theft the size of the box it was taken from, lets a single machine be
retired, and keeps the record honest about which of a person's minds actually
wrote something.

Not a JWT: the database is in this process, so stateless verification buys
nothing and costs revocation — which would then be a denylist, which is this
table again. `credential_for_token` reads the table on **every** request
rather than caching, because a revocation that needs a restart to land is not
a revocation.

## The shared token is not a credential any more

`COMMS_BEARER_TOKEN` is no longer accepted. It was one value presented by
every mind, every surface bot and two machines across the LAN, which made its
theft indistinguishable from its use and made per-holder revocation
meaningless while it stood. Presenting it resolves no holder and is refused
exactly like any other unrecognised value, and there is no flag that softens
that: a fallback nobody can observe is a fallback that never gets removed.

The cost is paid at deployment rather than hidden. Every holder is minted a
value and given it *before* this code runs; a holder that was missed is
refused the moment it does, loudly, rather than carrying on under a shared
credential whose scope nobody could describe. Two of them are the boys'
boxes, which cannot be reached over SSH and are expected to go dark until
somebody walks over to them.

`COMMS_PRIMARY_USER` survives with a narrower job than it had: it names who
owns a conversation whose mind cannot be resolved to an owner at all. It is
no longer anybody's credential.

## Classification is deny-by-default and covers websockets

`RouteTable` holds one `Rule` per route and `DEFAULT_RULE` is `ADMIN`, so a
route nobody classified is refused rather than served: forgetting to classify
a new route costs an admin-only route, never an open one. `unlisted` reports
what is unclassified so the suite can refuse to go green on it, and it
enumerates every route rather than everything under `/sessions`, because
`POST /command` carries `/kill`, `/prune` and `/switch` from a path that
prefix would miss.

The gate is pure ASGI rather than `@app.middleware("http")`. The HTTP decorator
does not wrap websocket connections at all, and the two routes most of this is
about — `/sessions/{id}/stream` and `/sessions/{id}/attach` — are websockets.
A classification enforced through an HTTP middleware is green in the suite and
absent on exactly the routes that matter.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import hive_data
from hive_data import broker_ops, core
from starlette.routing import BaseRoute
from starlette.types import Receive, Scope, Send

log = logging.getLogger("hive-mind.user_access")

# The store not answering, as opposed to answering no.
_STORE_DOWN = (
    hive_data.errors.Unreachable, hive_data.errors.TimedOut,
    hive_data.errors.NoConnectionAvailable, hive_data.errors.NotInitialised,
)
STORE_DOWN_BODY = b'{"detail":"the hive database is not answering"}'

# ---------------------------------------------------------------------------
# Route classes
# ---------------------------------------------------------------------------

#: No credential at all — health and liveness.
PUBLIC = "public"
#: The service or admin token, with no user scoping. The machinery each mind
#: runs: registration, context reports, turn recording, rotation arming and
#: firing, carry-forward. A mind is not a person and has no conversations of
#: its own to be scoped to.
MACHINE = "machine"
#: A recognised caller, whose handler filters its own results. Listings.
SCOPED = "scoped"
#: A recognised caller who must own the conversation named by `param`.
OWNER = "owner"
#: The admin token only.
ADMIN = "admin"
#: A change to one mind, asked with a person's key or a mind's key and nothing
#: else (feature 834, requirement 24). The gate resolves the caller with a
#: person's key admitted and refuses the admin credential, a service holder
#: and anything unrecognised; the handler decides whether this caller may
#: change that mind.
CHANGE = "change"

_KINDS = frozenset({PUBLIC, MACHINE, SCOPED, OWNER, ADMIN, CHANGE})

WEBSOCKET = "WS"

#: Where the gate leaves the caller it resolved, for handlers to filter on.
CALLER_SCOPE_KEY = "hive_caller"

# A refused websocket is refused with a real 401 through the ASGI
# denial-response extension. A pre-accept `close()` presents to the browser as
# HTTP 403, which is also what a route that does not exist answers — so a
# refused credential would read as "this gateway has no attach route" and send
# the operator off to redeploy something that is working.
WS_DENY_CODE = 4401


@dataclass(frozen=True)
class Rule:
    kind: str
    #: For `OWNER`: the path parameter naming the session.
    param: str | None = None
    #: True when a person's own key is also taken on this route (feature 834,
    #: story 905: everything a signed-in person does reaches the gateway with
    #: that person's key). `OWNER` and `SCOPED` routes take it always. On an
    #: `ADMIN` route it admits only a person who is an administrator.
    person: bool = False
    #: For a person-taking `ADMIN` route that the owner of a mind may also
    #: use: the path parameter naming the mind. A person who is not an
    #: administrator passes only when they own that mind.
    mind_param: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"unknown route class {self.kind!r}")
        if self.kind == OWNER and not self.param:
            raise ValueError("an owner rule names the parameter it resolves")


DEFAULT_RULE = Rule(ADMIN)


@dataclass
class RouteTable:
    rules: dict[tuple[str, str], Rule] = field(default_factory=dict)

    def add(self, rule: Rule, *routes: str) -> None:
        """Classify routes written as "METHOD /path/{param}" ("WS" for a
        websocket)."""
        for spec in routes:
            method, _, path = spec.partition(" ")
            key = (method.upper(), path)
            if key in self.rules:
                raise ValueError(f"{spec} is classified twice")
            self.rules[key] = rule

    def rule_for(self, method: str, path: str) -> Rule:
        # HEAD is served by the GET handler, so it answers to the GET rule.
        method = "GET" if method.upper() == "HEAD" else method.upper()
        return self.rules.get((method, path), DEFAULT_RULE)

    def unlisted(self, app: Any) -> list[tuple[str, str]]:
        """Every route the app serves that this table does not name."""
        missing = []
        for route in app.router.routes:
            for method in _route_methods(route):
                if (method, getattr(route, "path", "")) not in self.rules:
                    missing.append((method, getattr(route, "path", "")))
        return missing


def _route_methods(route: BaseRoute) -> list[str]:
    methods = getattr(route, "methods", None)
    if methods:
        return sorted(m for m in methods if m != "HEAD")
    # A websocket route has no `methods`. A `Mount` has none either, and is a
    # whole sub-application rather than a route this table can classify — it
    # must not be reported as a websocket, which is how a `StaticFiles` mount
    # would have had its entire subtree classified admin-only.
    from starlette.routing import Mount, WebSocketRoute  # noqa: PLC0415

    if isinstance(route, WebSocketRoute):
        return [WEBSOCKET]
    if isinstance(route, Mount):
        return []
    return [WEBSOCKET] if hasattr(route, "path") else []


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

#: What a holder *is*. A credential held by a mind or by a service is the
#: machinery talking; one held by a person is not, and the distinction decides
#: whether it may reach the routes addressed by client reference rather than
#: by conversation. Derived from the `minds` table at mint time rather than
#: declared twice, so a mind that is deleted stops being machinery on its own.
MIND = "mind"
SERVICE = "service"
PERSON = "person"
#: A person's own key, kept by the console and shown to nobody (feature 834,
#: requirement 19). It names the person as holder and username and opens
#: nothing at this gateway: `resolve_caller` gives it no standing on any route,
#: so a copy that leaks cannot read a conversation. Its one use is the change
#: route that forwards a change to a mind (`caller_may_change`).
USER_KEY = "user"
ROLES = (MIND, SERVICE, PERSON, USER_KEY)

#: Roles that may reach the routes the mind machinery drives.
MACHINE_ROLES = (MIND, SERVICE)

#: Not a holder. Lucent files hive-wide standing rules under this provenance,
#: and comms refuses to mint it for the same reason lucent does — so the two
#: services cannot disagree about whether it names a party.
SHARED_PROVENANCE = "shared"

@dataclass(frozen=True)
class Credential:
    """One holder's row, resolved from a presented value."""

    holder: str
    username: str
    role: str


def digest(token: str) -> str:
    """The stored form. A credential this process is handed is never written
    down in the form it arrived in, so a reader of the table cannot present
    what it finds there."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


#: The label the credential shown to a mind is made under. The same value is
#: in `core/mind_key.py` (the mind's side), and a test holds the two together.
GATEWAY_CREDENTIAL_LABEL = b"hive-mind: what the gateway shows this mind, v1"


def gateway_credential(mind_key_value: str) -> str:
    """What this gateway shows a mind on every call: made from the mind's key.

    HMAC-SHA256 of a fixed label under the key. It cannot be turned back into
    the key, and it is not the `digest` kept to recognise the key, so what a
    reader of the credentials table finds opens nothing here, and this value
    presented as a key is no key of any holder. Worked out when the key is
    seen (a mind registering with it, the key being replaced) and kept in
    `core.minds.session_token`; the raw key is never kept.
    """
    return hmac.new(str(mind_key_value).strip().encode("utf-8"),
                    GATEWAY_CREDENTIAL_LABEL, hashlib.sha256).hexdigest()


def normalize_holder(name: object) -> str:
    return str(name or "").strip().lower()


async def _is_registered_mind(holder: str) -> bool:
    return await core.get_mind_by_id_or_name(holder) is not None


async def put_credential(
    holder: str,
    username: str,
    token: str,
    role: str | None = None,
) -> dict:
    """Mint or replace one holder's credential.

    A holder is a mind, a service, or a person — never "a person's minds
    collectively". Two holders owned by one person are two rows with two
    values, which is the whole point: revoking the one that sits on a box in
    somebody's bedroom must not revoke the one running here.

    Replacing clears a revocation: the operator issuing a new value has not
    asked for it to arrive dead.
    """
    key = normalize_holder(holder)
    if not key:
        raise ValueError("a credential names the holder it belongs to")
    name = normalize_username(username)
    if not name:
        raise ValueError("a credential names the user its holder answers to")
    if not token:
        raise ValueError("a credential needs a value")
    if role is None:
        role = MIND if await _is_registered_mind(key) else PERSON
    if key == SHARED_PROVENANCE:
        raise ValueError(
            "'shared' is the provenance hive-wide rules are written under, "
            "not a holder"
        )
    if role not in ROLES:
        raise ValueError(f"unknown credential role: {role!r}")
    if role == USER_KEY:
        # A person's key is held under the person's own name, so "the key of
        # the user who owns this mind" is one lookup, and never under the name
        # of a mind, whose row it would take over.
        if key != name:
            raise ValueError("a user's key is held under that user's own name")
        if await _is_registered_mind(key):
            raise ValueError(f"{key!r} is a mind, not a user")
    sha = digest(token)
    clash = await broker_ops.holder_of_digest(sha)
    if clash is not None and clash != key:
        # Checked rather than left to the unique index so the console gets a
        # sentence instead of a constraint violation, and names the holder it
        # collided with: the operator has almost certainly pasted one
        # machine's value into another's field, and needs to know which.
        # Revoked holders count — the digest is unique across every row.
        raise ValueError(_held_by(clash))
    now = time.time()
    try:
        # One unit: the key and what the gateway shows the mind (made from it)
        # change together or not at all, so a failure between the two cannot
        # leave the mind refusing the gateway with the new key live.
        async with hive_data.transaction() as unit:
            await core.upsert_credential(
                holder=key, username=name, role=role, token_sha=sha,
                created_at=now, revoked_at=None, unit=unit,
            )
            if role == MIND:
                # The mind's key was just replaced (or made): the next call
                # carries the value made from the new key and the old one is
                # refused, with no step by a person.
                known = await core.get_mind_by_id_or_name(key, unit=unit)
                if known is not None:
                    await broker_ops.update_mind(
                        known["mind_id"], unit=unit,
                        session_token=gateway_credential(token))
    except hive_data.Conflict as exc:
        # The same value minted for another holder between the check and the
        # write. Still a sentence, not a 500.
        raise ValueError(
            _held_by(await broker_ops.holder_of_digest(sha) or "another holder")
        ) from exc
    return {"holder": key, "username": name, "role": role, "created_at": now}


def _held_by(holder: str) -> str:
    return (
        f"that value is already held by {holder!r}; "
        "a credential belongs to one holder"
    )


async def revoke_credential(holder: str) -> bool:
    """Mark one holder's credential dead, and only that holder's.

    The row is kept rather than deleted so the console can say "revoked"
    where it would otherwise have to say "never had one", which are different
    sentences with different remedies.
    """
    key = normalize_holder(holder)
    result = await broker_ops.revoke_live_credential(key, time.time())
    return result["rows"] > 0


async def remove_mind_credential(holder: str) -> bool:
    """Delete a mind's credential row outright, freeing its digest.

    For a creation that failed and a mind that was deleted. A revoked row
    keeps the digest and refuses it to every other holder for good; a key
    cut off from a mind stays revoked, but one that never made a mind, or
    belongs to one that is gone, must be pasteable again. Only a row typed
    `mind` is removed.
    """
    key = normalize_holder(holder)
    result = await broker_ops.delete_mind_credential(key)
    return result["rows"] > 0


async def credential_for_token(token: str) -> Credential | None:
    """The holder a presented credential belongs to, or None.

    Read per call, never cached: requirement is that a revocation lands on the
    holder's next request without a restart, and a cache is exactly the thing
    that would make that false while every test of the table itself stayed
    green.
    """
    if not token:
        return None
    # Looked up by digest. The old walk compared every live row in constant
    # time; an indexed lookup on a SHA-256 digest of a random value leaks
    # nothing a caller can steer, since nobody chooses the digest's prefix.
    row = await core.credential_for_token(digest(token))
    if row is None:
        return None
    return Credential(
        holder=normalize_holder(row["holder"]),
        username=normalize_username(row["username"]),
        role=str(row["role"] or PERSON),
    )


async def user_for_token(token: str) -> str | None:
    """The user a presented credential answers for, or None.

    Kept at this signature because ownership is still decided by user: the
    holder is which box is talking, the user is whose things it may touch.
    """
    found = await credential_for_token(token)
    return found.username if found else None


def normalize_username(name: object) -> str:
    return str(name or "").strip().lower()


def primary_user() -> str:
    """Who owns a conversation whose mind cannot be resolved to an owner.

    This was once the identity the shared `COMMS_BEARER_TOKEN` stood for.
    That token is no longer a credential at all, so what is left is the
    narrower job the same value was already doing: `sessions.mind_id` carried
    a `DEFAULT 'ada'` for most of this schema's life, and the live table still
    holds rows naming a mind that is unregistered, renamed or gone. Refusing
    those to everyone would take the operator's own history away with no
    migration and no message, so they answer to the user named here, who on a
    hive with one operator is whose they were.

    Empty means nobody: an unresolvable conversation is then refused to all.
    """
    return normalize_username(os.environ.get("COMMS_PRIMARY_USER", ""))


# ---------------------------------------------------------------------------
# The caller
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Caller:
    """Who comms decided is asking."""

    username: str = ""
    #: Which box is talking: a mind, a service, or a person. The user above is
    #: whose things it may touch; this is who presented the value, and it is
    #: where a route addressed by mind rather than by conversation gets its
    #: mind from. Empty for the admin credential, which is every holder at
    #: once and therefore none of them.
    holder: str = ""
    #: One of `MIND`, `SERVICE`, `PERSON`, or empty when no credential row
    #: resolved.
    role: str = ""
    is_admin: bool = False
    #: True when the caller presented the shared service token. Tracked
    #: separately from `username`, because that token is two things at once on
    #: a hive naming a primary user: the machinery's credential *and* that
    #: person's. Folding them together is how a per-user credential got
    #: admitted to every machinery route.
    is_service: bool = False
    #: True when the caller reaches every conversation: the admin token, or
    #: the shared token on a hive that has named no primary user.
    unscoped: bool = False
    #: True when the caller is a person's key and that person is an
    #: administrator, read from the hive database on this request (feature
    #: 834, story 905). It reaches what the admin credential reaches on the
    #: routes that take a person's key, as that person and nobody else.
    person_admin: bool = False

    @property
    def recognised(self) -> bool:
        return bool(self.username) or self.is_admin or self.unscoped

    @property
    def is_machine(self) -> bool:
        """May reach the routes the mind machinery drives.

        Deliberately narrower than `recognised`. Those routes are addressed by
        `(client_type, client_ref)` or by mind rather than by a conversation,
        so there is nothing for an owner check to scope — which means a
        credential admitted here reaches every conversation on the hive
        through them. A person's credential is not admitted.
        """
        return (
            self.is_admin
            or self.is_service
            or self.unscoped
            or self.role in MACHINE_ROLES
        )

    def sees_everything(self) -> bool:
        return self.is_admin or self.unscoped or self.person_admin


def bearer_of(scope: Scope) -> str:
    for key, value in scope.get("headers") or []:
        if key == b"authorization":
            text = value.decode("latin-1")
            return text[7:].strip() if text.startswith("Bearer ") else ""
    return ""


async def resolve_caller(token: str, *, allow_person_key: bool = False) -> Caller:
    """Turn a presented bearer into a caller.

    `allow_person_key` is for the one route class that takes a person's key
    (`CHANGE`); everywhere else a person's key resolves to nobody.

    No credential of the gateway's own stands for every holder: an
    administrator is a person's key whose person is an administrator.

    Everything resolves out of the credentials table and nowhere else.
    The shared `COMMS_BEARER_TOKEN` is deliberately absent: one value that
    every mind, every surface bot and two boxes in the boys' bedrooms all
    presented is a value whose theft is indistinguishable from its use, and
    while it was accepted no per-holder revocation could mean anything.
    Presenting it now resolves no holder and is refused like any other
    unrecognised value.

    There is no bypass for a gateway with nothing configured. An absent
    credential store is a gateway that cannot establish who is calling, and
    serving every route open while it works that out is how a hive stays
    green for a week with its front door off its hinges.
    """
    if not token:
        return Caller()
    found = await credential_for_token(token)
    if not found:
        return Caller()
    if found.role == USER_KEY:
        if allow_person_key:
            return await _person_caller(found)
        # A person's key for their minds' settings opens no route here.
        return Caller()
    return Caller(
        username=found.username, holder=found.holder, role=found.role
    )


async def _person_caller(found) -> Caller:
    """A person's key as a caller: that person, and whether they are an
    administrator, both read from the hive database on this request. A person
    whose account is disabled is nobody."""
    row = await _live_user_row(found.username)
    if row is not None and row["disabled_at"] is not None:
        return Caller()
    admin = await person_is_administrator(found.username)
    return Caller(username=found.username, holder=found.holder, role=USER_KEY,
                  person_admin=admin)


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


async def owner_of_mind(mind_id: str) -> str | None:
    """The user a mind belongs to, by `mind_id` or by name.

    By name as well, because `sessions.mind_id` carried a `DEFAULT 'ada'` for
    most of this schema's life and the live table still holds rows whose
    `mind_id` is a mind's *name*. Keying on the uuid alone would make every one
    of those rows ownerless, which under the refusal rule means the operator's
    own history answers "no such conversation".
    """
    key = str(mind_id or "").strip()
    if not key:
        return None
    # `name` is stored as registered and compared byte-wise, so a mind
    # registered as "Hex" would never match a lowered key. Comparing both
    # sides lowered is what keeps this agreeing with `visible_minds`, which
    # lowers too — disagreeing meant a picker drew names whose every route
    # then answered 404.
    row = await core.get_mind_by_id_or_name(key)
    if not row:
        return None
    return normalize_username(row["owner"]) or None


#: The role slug the hive records for an operator mind (the same slug the
#: console's role pages and `GET /roles/operator/holders` use).
OPERATOR_ROLE = "operator"


async def is_operator_mind(holder: str) -> bool:
    """Whether the mind this holder names holds the operator role right now.

    Read from the roles tables on every call, never cached: a role taken away
    on the console makes the mind's key an ordinary mind's key on its next
    request (feature 834, requirement 20).
    """
    from hive_data import roles

    try:
        row = await core.get_mind_by_id_or_name(str(holder or "").strip())
        if not row:
            return False
        return any(r["slug"] == OPERATOR_ROLE
                   for r in await roles.roles_of(row["mind_id"]))
    except Exception:  # noqa: BLE001 - an unreadable role is no operator (fail closed)
        log.warning("operator role could not be read; treated as not an operator",
                    exc_info=True)
        return False


async def operator_may_change(operator_holder: str, target: str) -> bool:
    """Whether an operator mind may change this other mind's settings.

    The smallest safe reading (Cypher, requirement 20): only a mind owned by
    the same person as the operator mind, as the registry records it. Never
    another person's mind, never an ownerless one, the mind itself passes trivially (its own key already speaks for it).
    """
    if not await is_operator_mind(operator_holder):
        return False
    mine = await owner_of_mind(operator_holder)
    theirs = await owner_of_mind(target)
    return bool(mine and theirs and mine == theirs)


ADMINS_GROUP = "admins"


async def _live_user_row(username: str):
    name = normalize_username(username)
    if not name:
        return None
    for row in await core.list_users():
        if normalize_username(row["username"]) == name:
            return row
    return None


async def person_is_administrator(username: str) -> bool:
    """Whether this person is in the admins group, read from the hive database
    on this call (feature 834, requirement 32). A person with no row, a
    disabled one, or a database that cannot be read is not an administrator."""
    name = normalize_username(username)
    if not name:
        return False
    try:
        for row in await core.list_users():
            if normalize_username(row["username"]) == name:
                if row["disabled_at"] is not None:
                    return False
                return ADMINS_GROUP in await core.groups_of(row["id"])
    except _STORE_DOWN:
        raise
    except Exception:  # noqa: BLE001 - an unreadable group is no administrator (fail closed)
        log.warning("admins group could not be read; treated as not an administrator",
                    exc_info=True)
    return False


async def caller_may_change(caller: Caller, target: dict) -> bool:
    """Whether this caller may change this mind (requirement 24).

    A person who is an administrator: any mind. Any other person: a mind they
    own (an ownerless mind follows the conversation rule and answers to the
    primary user). An operator mind: a mind owned by the same person as
    itself. An ordinary mind: itself only. Everything read on this call.
    """
    holder = normalize_holder(caller.holder)
    if caller.role == USER_KEY:
        # A person whose account is disabled changes nothing, whatever their
        # key still says; a person with no account row (a hive that records
        # people elsewhere) is judged on ownership alone.
        row = await _live_user_row(caller.username)
        if row is not None and row["disabled_at"] is not None:
            return False
        if await person_is_administrator(caller.username):
            return True
        owner = normalize_username(target.get("owner"))
        if owner:
            return owner == caller.username
        return caller.username == primary_user()
    if caller.role == MIND:
        names = {normalize_holder(target.get("mind_id")), normalize_holder(target.get("name"))}
        if holder in names:
            return True
        return await operator_may_change(holder, str(target.get("mind_id") or ""))
    return False


async def owns_mind(caller: Caller, mind_id: str) -> bool:
    if caller.sees_everything():
        return True
    if not caller.username:
        return False
    owner = await owner_of_mind(mind_id)
    if owner is None:
        # A conversation whose mind is unregistered, renamed, deleted, or
        # still carrying the old `mind_id DEFAULT 'ada'` has no owner to
        # derive. Refusing it to everyone means the operator loses their own
        # history the moment scoping is switched on, with no migration and no
        # message; so an ownerless conversation answers to the primary user,
        # who on a hive with one operator is whose it was.
        return caller.username == primary_user()
    return owner == caller.username


async def holder_keys(caller: Caller) -> set[str] | None:
    """Every key naming the one mind this caller *is*, or None for "any".

    `owns_mind` answers a question about a person, which is the right question
    for a conversation: a surface bot serves several of its owner's minds and
    must reach all of them. The machinery routes ask something narrower. They
    are addressed by client reference rather than by conversation, so there is
    no conversation for an owner check to scope — and with twelve minds on
    this hive owned by three people, an owner check there lets any one of
    Daniel's ten minds append a turn to any other's ledger, where
    `get_late_turns` folds it into the next conversation as something he
    typed.

    So a `mind` holder is held to its own mind and nothing else. None means no
    restriction, which is the admin credential and a `service` holder — comms
    itself drives these routes on every mind's behalf.
    """
    if caller.sees_everything() or caller.role != MIND:
        return None
    keys = {caller.holder}
    row = await core.get_mind_by_id_or_name(caller.holder)
    if row:
        for key in (str(row["mind_id"] or ""), normalize_username(row["name"])):
            if key:
                keys.add(key)
    return keys


def mind_is_named_in(keys: set[str] | None, mind_id: object) -> bool:
    """Whether a row's mind is one of `keys`, with None meaning "any"."""
    if keys is None:
        return True
    return _named_in(mind_id, keys)


async def all_mind_keys() -> set[str]:
    """Every key naming any registered mind, whoever owns it.

    `filter_sessions` needs this to tell an *ownerless* conversation from
    *somebody else's*. Judging that against the caller's own keys would hand
    every other user's conversation to the primary user, which is the opposite
    of the point.
    """
    keys: set[str] = set()
    for row in await core.list_minds():
        for key in (str(row["mind_id"] or ""), normalize_username(row["name"])):
            if key:
                keys.add(key)
    return keys


async def visible_minds(caller: Caller) -> set[str]:
    """Every key — uuid and name — naming a mind this caller owns.

    Both forms, for the same reason `owner_of_mind` accepts both: a session row
    may name its mind either way.
    """
    keys: set[str] = set()
    for row in await core.list_minds():
        if caller.sees_everything() or normalize_username(row["owner"]) == caller.username:
            # An empty key would match every session whose `mind_id` is null
            # or blank, handing them to whoever owned the nameless row.
            for key in (str(row["mind_id"] or ""), normalize_username(row["name"])):
                if key:
                    keys.add(key)
    return keys


def filter_sessions(
    sessions: Iterable[dict],
    caller: Caller,
    keys: set[str],
    known: set[str] | None = None,
) -> list[dict]:
    """The caller's own conversations, out of the rows a manager returned.

    `keys` holds every uuid and lowered name naming a mind the caller owns,
    both forms because a session row may name its mind either way. `known`
    holds the same for every registered mind, and is what distinguishes an
    *ownerless* conversation from somebody else's: ownerless rows go to the
    primary user, exactly as `owns_mind` decides, so a listing never hides a
    row whose routes would serve it.
    """
    if caller.sees_everything():
        return list(sessions)
    primary = bool(caller.username) and caller.username == primary_user()
    registered = known if known is not None else keys
    out = []
    for session in sessions:
        mind = session.get("mind_id")
        if _named_in(mind, keys):
            out.append(session)
        elif primary and not _named_in(mind, registered):
            out.append(session)
    return out


def _named_in(mind: object, keys: set[str]) -> bool:
    return str(mind or "") in keys or normalize_username(mind) in keys


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

#: A refusal is shaped like the answer for a conversation that does not exist,
#: so the ids of other people's conversations cannot be found by probing. 404
#: is what `GET /sessions/{id}` already answers for an absent row, and this is
#: the one shape every person-driven route is made to share.
NOT_FOUND_BODY = b'{"error":"Session not found"}'


def _store_open() -> bool:
    try:
        hive_data.live_pool()
    except hive_data.NotInitialised:
        return False
    return True


class UserAccess:
    """Pure-ASGI gate: resolves the caller, enforces the route's class.

    Pure ASGI rather than `@app.middleware("http")` so that websocket
    connections pass through it. The HTTP decorator does not see them, and the
    two websocket routes are the ones this exists for.

    The readiness test and the session lookup arrive as callables rather
    than as imports so the gate can be driven in a test without standing up
    a gateway. Readiness defaults to whether `hive_data` has a pool open,
    which is what the gateway's lifespan opens before anything serves and
    closes after it stops.
    """

    def __init__(
        self,
        app,
        table: RouteTable,
        *,
        session_mind,
        ready=None,
    ) -> None:
        self._app = app
        self._table = table
        #: () -> bool: whether the credential store can be read yet
        self._ready = ready if ready is not None else _store_open
        #: async (session_id) -> mind_id | None, resolving short ids too
        self._session_mind = session_mind

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return
        route, params = self._match(scope)
        if route is None:
            # Nothing this app serves. Passing it through lets the router give
            # its own answer — a 404, a 405, or the 307 that `redirect_slashes`
            # adds for a trailing slash. Refusing instead told a client that
            # typed one slash too many that its credential was bad.
            await self._app(scope, receive, send)
            return
        method = WEBSOCKET if kind == "websocket" else scope.get("method", "GET")
        path = getattr(route, "path", scope.get("path", ""))
        rule = self._table.rule_for(method, path)
        if rule.kind == PUBLIC:
            await self._app(scope, receive, send)
            return
        if not self._ready():
            # The gateway is still starting, or shutting down. Serving a
            # session route with no way to resolve a caller is the one thing
            # this must never do, so it refuses rather than passing through.
            await self._refuse(scope, receive, send, 503, b'{"detail":"starting"}')
            return
        takes_person = (rule.person or rule.kind in (OWNER, SCOPED, CHANGE))
        try:
            caller = await resolve_caller(bearer_of(scope), allow_person_key=takes_person)
        except _STORE_DOWN:
            # A database that did not answer is not a bad credential and not
            # a server bug: every authenticated request and websocket
            # handshake would otherwise come back 500 while Postgres restarts.
            await self._refuse(scope, receive, send, 503, STORE_DOWN_BODY)
            return
        if rule.kind == ADMIN:
            if not (rule.person and caller.person_admin):
                if not (rule.person and rule.mind_param and caller.role == USER_KEY
                        and await self._person_owns(caller, params.get(rule.mind_param) or "")):
                    await self._refuse(scope, receive, send, 401, b'{"detail":"Invalid token"}')
                    return
        elif rule.kind == CHANGE:
            # A person's key or a mind's key, and nothing else. The admin
            # credential and every service holder are refused here: no
            # credential of the hive's own may stand for anyone.
            try:
                caller = await resolve_caller(bearer_of(scope), allow_person_key=True)
            except _STORE_DOWN:
                await self._refuse(scope, receive, send, 503, STORE_DOWN_BODY)
                return
            if (caller.is_admin or caller.is_service or caller.unscoped
                    or caller.role not in (MIND, USER_KEY) or not caller.holder):
                await self._refuse(scope, receive, send, 401, b'{"detail":"Invalid token"}')
                return
        elif rule.kind == MACHINE:
            # Not `recognised`. These routes are addressed by client reference
            # or by mind, so there is no conversation for an owner check to
            # scope — admitting a person's credential here would hand them
            # every conversation on the hive through the back of the house.
            if not caller.is_machine and not (rule.person and caller.role == USER_KEY):
                await self._refuse(scope, receive, send, 401, b'{"detail":"Invalid token"}')
                return
        elif not caller.recognised:
            await self._refuse(scope, receive, send, 401, b'{"detail":"Invalid token"}')
            return
        if rule.kind == OWNER and not caller.sees_everything():
            session_id = params.get(rule.param or "") or ""
            try:
                mind_id = await self._session_mind(session_id)
            except _STORE_DOWN:
                # A read that failed is not an absent row: answering 404 would
                # tell the user their conversation does not exist.
                await self._refuse(scope, receive, send, 503, STORE_DOWN_BODY)
                return
            # An absent session and someone else's session answer alike. The
            # handler would 404 the first anyway; what matters is that the
            # second does not answer differently and reveal that it exists.
            if mind_id is None or not await owns_mind(caller, mind_id):
                await self._refuse(scope, receive, send, 404, NOT_FOUND_BODY)
                return
        scope[CALLER_SCOPE_KEY] = caller
        await self._app(scope, receive, send)

    async def _person_owns(self, caller: Caller, mind: str) -> bool:
        """Whether this person may change the mind named: its owner (an
        ownerless mind answers to the primary user)."""
        try:
            row = await core.get_mind_by_id_or_name(str(mind or "").strip())
        except _STORE_DOWN:
            raise
        return row is not None and await caller_may_change(caller, dict(row))

    def _match(self, scope: Scope):
        from starlette.routing import Match  # noqa: PLC0415

        app = scope.get("app")
        if app is None:
            return None, {}
        for route in app.router.routes:
            match, child = route.matches(scope)
            if match == Match.FULL:
                return route, dict(child.get("path_params") or {})
        return None, {}

    async def _refuse(
        self, scope: Scope, receive: Receive, send: Send, status: int, body: bytes
    ) -> None:
        if scope.get("type") != "websocket":
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        # A websocket refused before the handshake completes. The denial
        # response extension answers with a real status; without it, a
        # pre-accept close is the only option and presents as HTTP 403 —
        # indistinguishable from a route that is not there.
        if "websocket.http.response" in (scope.get("extensions") or {}):
            await send(
                {
                    "type": "websocket.http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "websocket.http.response.body", "body": body})
            return
        await send({"type": "websocket.close", "code": WS_DENY_CODE})
