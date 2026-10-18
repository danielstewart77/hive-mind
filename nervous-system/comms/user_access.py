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
opaque random value, stored here as a SHA-256 digest in `broker.db` and held
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
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import aiosqlite
from starlette.routing import BaseRoute
from starlette.types import Receive, Scope, Send

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

_KINDS = frozenset({PUBLIC, MACHINE, SCOPED, OWNER, ADMIN})

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
ROLES = (MIND, SERVICE, PERSON)

#: Roles that may reach the routes the mind machinery drives.
MACHINE_ROLES = (MIND, SERVICE)

#: Not a holder. Lucent files hive-wide standing rules under this provenance,
#: and comms refuses to mint it for the same reason lucent does — so the two
#: services cannot disagree about whether it names a party.
SHARED_PROVENANCE = "shared"

CREDENTIALS_DDL = """
CREATE TABLE IF NOT EXISTS credentials (
    -- NOT NULL is load-bearing: SQLite permits NULL in a non-INTEGER PRIMARY
    -- KEY column unless it is declared, so without it a row naming no holder
    -- at all inserts cleanly and then resolves by digest exactly as a real
    -- one does. Every gate test would stay green against a credential that
    -- belongs to nobody.
    holder      TEXT PRIMARY KEY NOT NULL,
    username    TEXT NOT NULL,
    role        TEXT NOT NULL,
    -- UNIQUE because "no two holders share one value" is the whole point and
    -- is otherwise a copy-paste away: `credential_for_token` walks the rows
    -- and returns the first digest that matches, so the second holder to be
    -- issued a duplicate silently authenticates as the first one's owner.
    -- One row per person made that impossible to express; one row per holder
    -- does not.
    token_sha   TEXT NOT NULL UNIQUE,
    created_at  REAL NOT NULL,
    revoked_at  REAL
);
CREATE INDEX IF NOT EXISTS credentials_username ON credentials (username);
"""


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


def normalize_holder(name: object) -> str:
    return str(name or "").strip().lower()


async def init_credentials(db: aiosqlite.Connection) -> None:
    """Create the table, and carry across anything the one-per-person table
    held.

    The predecessor keyed on `username`, which is exactly the thing this
    change stops doing: a person had one value, so lifting it off any box
    they owned was lifting their whole identity. SQLite cannot move a primary
    key in place, so the rows are copied — each old row becomes a holder
    named after the person who held it, with the role `person`, which is what
    it actually was. The old table is left on disk rather than dropped: it
    holds digests, not values, and a rollback that found it missing would
    leave every holder refused with nothing to restore.
    """
    await db.executescript(CREDENTIALS_DDL)
    rows = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_credentials'"
    )
    if await rows.fetchone():
        await db.execute(
            "INSERT OR IGNORE INTO credentials "
            "(holder, username, role, token_sha, created_at, revoked_at) "
            "SELECT username, username, ?, token_sha, created_at, revoked_at "
            "FROM user_credentials "
            "WHERE username IS NOT NULL AND trim(username) <> ''",
            (PERSON,),
        )
    await db.commit()


async def _is_registered_mind(db: aiosqlite.Connection, holder: str) -> bool:
    rows = await db.execute(
        "SELECT 1 FROM minds WHERE mind_id = ? OR lower(name) = ? LIMIT 1",
        (holder, holder.lower()),
    )
    return await rows.fetchone() is not None


async def put_credential(
    db: aiosqlite.Connection,
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
        role = MIND if await _is_registered_mind(db, key) else PERSON
    if key == SHARED_PROVENANCE:
        raise ValueError(
            "'shared' is the provenance hive-wide rules are written under, "
            "not a holder"
        )
    if role not in ROLES:
        raise ValueError(f"unknown credential role: {role!r}")
    sha = digest(token)
    clash = await db.execute(
        "SELECT holder FROM credentials WHERE token_sha = ? AND holder <> ?",
        (sha, key),
    )
    row = await clash.fetchone()
    if row:
        # Checked rather than left to the UNIQUE constraint so the console
        # gets a sentence instead of an IntegrityError, and names the holder
        # it collided with: the operator has almost certainly pasted one
        # machine's value into another's field, and needs to know which.
        raise ValueError(
            f"that value is already held by {row[0]!r}; "
            "a credential belongs to one holder"
        )
    now = time.time()
    await db.execute(
        "INSERT INTO credentials "
        "(holder, username, role, token_sha, created_at, revoked_at) "
        "VALUES (?, ?, ?, ?, ?, NULL) "
        "ON CONFLICT(holder) DO UPDATE SET "
        "username=excluded.username, role=excluded.role, "
        "token_sha=excluded.token_sha, created_at=excluded.created_at, "
        "revoked_at=NULL",
        (key, name, role, sha, now),
    )
    await db.commit()
    return {"holder": key, "username": name, "role": role, "created_at": now}


async def revoke_credential(db: aiosqlite.Connection, holder: str) -> bool:
    """Mark one holder's credential dead, and only that holder's.

    The row is kept rather than deleted so the console can say "revoked"
    where it would otherwise have to say "never had one", which are different
    sentences with different remedies.
    """
    key = normalize_holder(holder)
    cursor = await db.execute(
        "UPDATE credentials SET revoked_at = ? "
        "WHERE holder = ? AND revoked_at IS NULL",
        (time.time(), key),
    )
    await db.commit()
    return cursor.rowcount > 0


async def credential_for_token(
    db: aiosqlite.Connection, token: str
) -> Credential | None:
    """The holder a presented credential belongs to, or None.

    Read per call, never cached: requirement is that a revocation lands on the
    holder's next request without a restart, and a cache is exactly the thing
    that would make that false while every test of the table itself stayed
    green.
    """
    if not token:
        return None
    rows = await db.execute(
        "SELECT holder, username, role, token_sha FROM credentials "
        "WHERE revoked_at IS NULL"
    )
    presented = digest(token)
    for holder, username, role, token_sha in await rows.fetchall():
        # Constant-time, on bytes. `compare_digest` raises TypeError on a str
        # holding anything outside ASCII, and on a websocket handshake that
        # 500 reaches the browser as "this gateway has no such route".
        if hmac.compare_digest(
            presented.encode("ascii"), str(token_sha).encode("ascii")
        ):
            return Credential(
                holder=normalize_holder(holder),
                username=normalize_username(username),
                role=str(role or PERSON),
            )
    return None


async def user_for_token(db: aiosqlite.Connection, token: str) -> str | None:
    """The user a presented credential answers for, or None.

    Kept at this signature because ownership is still decided by user: the
    holder is which box is talking, the user is whose things it may touch.
    """
    found = await credential_for_token(db, token)
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
        return self.is_admin or self.unscoped


def bearer_of(scope: Scope) -> str:
    for key, value in scope.get("headers") or []:
        if key == b"authorization":
            text = value.decode("latin-1")
            return text[7:].strip() if text.startswith("Bearer ") else ""
    return ""


async def resolve_caller(db: aiosqlite.Connection, token: str) -> Caller:
    """Turn a presented bearer into a caller.

    The admin credential is checked first, because the console and the
    operator must keep a way into a wedged conversation whatever else is
    configured, and because it is the one credential that stands for every
    holder at once — the console serves whoever is logged in and does its own
    filtering from that session.

    Everything else resolves out of the credentials table and nowhere else.
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
    admin = os.environ.get("COMMS_ADMIN_BEARER_TOKEN", "")
    if not token:
        return Caller()
    if admin and hmac.compare_digest(token.encode("utf-8"), admin.encode("utf-8")):
        return Caller(is_admin=True, is_service=True, unscoped=True)
    found = await credential_for_token(db, token)
    if not found:
        return Caller()
    return Caller(
        username=found.username, holder=found.holder, role=found.role
    )


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


async def owner_of_mind(db: aiosqlite.Connection, mind_id: str) -> str | None:
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
    # `name` is stored as registered and SQLite compares TEXT byte-wise, so a
    # mind registered as "Hex" would never match a lowered key. Comparing both
    # sides lowered is what keeps this agreeing with `visible_minds`, which
    # lowers too — disagreeing meant a picker drew names whose every route
    # then answered 404.
    rows = await db.execute(
        "SELECT owner FROM minds WHERE mind_id = ? OR lower(name) = ?",
        (key, key.lower()),
    )
    row = await rows.fetchone()
    if not row:
        return None
    return normalize_username(row[0]) or None


async def owns_mind(
    db: aiosqlite.Connection, caller: Caller, mind_id: str
) -> bool:
    if caller.sees_everything():
        return True
    if not caller.username:
        return False
    owner = await owner_of_mind(db, mind_id)
    if owner is None:
        # A conversation whose mind is unregistered, renamed, deleted, or
        # still carrying the old `mind_id DEFAULT 'ada'` has no owner to
        # derive. Refusing it to everyone means the operator loses their own
        # history the moment scoping is switched on, with no migration and no
        # message; so an ownerless conversation answers to the primary user,
        # who on a hive with one operator is whose it was.
        return caller.username == primary_user()
    return owner == caller.username


async def holder_keys(
    db: aiosqlite.Connection, caller: Caller
) -> set[str] | None:
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
    rows = await db.execute(
        "SELECT mind_id, name FROM minds WHERE mind_id = ? OR lower(name) = ?",
        (caller.holder, caller.holder),
    )
    row = await rows.fetchone()
    if row:
        for key in (str(row[0] or ""), normalize_username(row[1])):
            if key:
                keys.add(key)
    return keys


def mind_is_named_in(keys: set[str] | None, mind_id: object) -> bool:
    """Whether a row's mind is one of `keys`, with None meaning "any"."""
    if keys is None:
        return True
    return _named_in(mind_id, keys)


async def all_mind_keys(db: aiosqlite.Connection) -> set[str]:
    """Every key naming any registered mind, whoever owns it.

    `filter_sessions` needs this to tell an *ownerless* conversation from
    *somebody else's*. Judging that against the caller's own keys would hand
    every other user's conversation to the primary user, which is the opposite
    of the point.
    """
    rows = await db.execute("SELECT mind_id, name FROM minds")
    keys: set[str] = set()
    for mind_id, name in await rows.fetchall():
        for key in (str(mind_id or ""), normalize_username(name)):
            if key:
                keys.add(key)
    return keys


async def visible_minds(
    db: aiosqlite.Connection, caller: Caller
) -> set[str]:
    """Every key — uuid and name — naming a mind this caller owns.

    Both forms, for the same reason `owner_of_mind` accepts both: a session row
    may name its mind either way.
    """
    rows = await db.execute("SELECT mind_id, name, owner FROM minds")
    keys: set[str] = set()
    for mind_id, name, owner in await rows.fetchall():
        if caller.sees_everything() or normalize_username(owner) == caller.username:
            # An empty key would match every session whose `mind_id` is null
            # or blank, handing them to whoever owned the nameless row.
            for key in (str(mind_id or ""), normalize_username(name)):
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


class UserAccess:
    """Pure-ASGI gate: resolves the caller, enforces the route's class.

    Pure ASGI rather than `@app.middleware("http")` so that websocket
    connections pass through it. The HTTP decorator does not see them, and the
    two websocket routes are the ones this exists for.

    The database handles and the session lookup arrive as callables rather
    than as imports so the gate can be driven in a test without standing up
    a gateway, and so it holds no opinion about where either lives.
    """

    def __init__(
        self,
        app,
        table: RouteTable,
        *,
        broker_db,
        session_mind,
    ) -> None:
        self._app = app
        self._table = table
        #: () -> aiosqlite.Connection | None
        self._broker_db = broker_db
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
        db = self._broker_db()
        if db is None:
            # The gateway is still starting, or shutting down. Serving a
            # session route with no way to resolve a caller is the one thing
            # this must never do, so it refuses rather than passing through.
            await self._refuse(scope, receive, send, 503, b'{"detail":"starting"}')
            return
        caller = await resolve_caller(db, bearer_of(scope))
        if rule.kind == ADMIN:
            if not os.environ.get("COMMS_ADMIN_BEARER_TOKEN", ""):
                # The same sentence `require_admin_bearer` gives. A 401 here
                # would send the operator to check a credential when the
                # problem is one unset variable.
                await self._refuse(
                    scope, receive, send, 503,
                    b'{"detail":"Admin endpoints disabled: '
                    b'COMMS_ADMIN_BEARER_TOKEN unset"}',
                )
                return
            if not caller.is_admin:
                await self._refuse(scope, receive, send, 401, b'{"detail":"Invalid token"}')
                return
        elif rule.kind == MACHINE:
            # Not `recognised`. These routes are addressed by client reference
            # or by mind, so there is no conversation for an owner check to
            # scope — admitting a person's credential here would hand them
            # every conversation on the hive through the back of the house.
            if not caller.is_machine:
                await self._refuse(scope, receive, send, 401, b'{"detail":"Invalid token"}')
                return
        elif not caller.recognised:
            await self._refuse(scope, receive, send, 401, b'{"detail":"Invalid token"}')
            return
        if rule.kind == OWNER and not caller.sees_everything():
            session_id = params.get(rule.param or "") or ""
            mind_id = await self._session_mind(session_id)
            # An absent session and someone else's session answer alike. The
            # handler would 404 the first anyway; what matters is that the
            # second does not answer differently and reveal that it exists.
            if mind_id is None or not await owns_mind(db, caller, mind_id):
                await self._refuse(scope, receive, send, 404, NOT_FOUND_BODY)
                return
        scope[CALLER_SCOPE_KEY] = caller
        await self._app(scope, receive, send)

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
