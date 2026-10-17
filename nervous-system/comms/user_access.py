"""Who is asking, and which conversations are theirs.

Comms has never had a notion of a person. Its three credentials — the service
token every surface bot and every mind holds, the admin token, and the rename
token — all answer *what* is calling and none of them answer *who for*. So
every session route has been open to every caller that holds any of them, and
a second person on the hive would see the first person's conversations.

A conversation's owner is **derived, never stored**: a session names a mind,
and `broker.minds` names that mind's owner. There is no owner column on
`sessions`, because a second place to record the same fact is a second place
for it to be wrong, and a mind handed to another user would leave every one
of its past conversations claiming the old one.

## The credential

A per-user credential, opaque and random, stored here as a SHA-256 digest in
`broker.db` and never held in plaintext by anything but the holder. Not a JWT:
the database is in this process, so stateless verification buys nothing and
costs revocation — which would then be a denylist, which is this table again.
`user_for_token` reads the table on **every** request rather than caching,
because a revocation that needs a restart to land is not a revocation.

## The existing shared token keeps working, and that is deliberate

`COMMS_PRIMARY_USER` names the user the shared `COMMS_BEARER_TOKEN` stands
for. Set, that value is the named user's own credential at its existing value:
every bot and every mind on the primary operator's hive goes on presenting
exactly what it presented yesterday, and nothing is re-tokened. Unset, the
shared token is **unscoped** — accepted on every route exactly as it is today,
reaching every conversation.

Unset meaning "as before" rather than "refused" is the whole rollout. A hive
upgrading to this code with no new configuration behaves identically to the
one it replaced, and setting one variable is what switches scoping on. The
alternative — scoping the moment the code lands — takes the operator's own
surfaces down at the instant they lose the ability to log in and fix it, which
is the one failure this module must not have.

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
    # A websocket route has no `methods`; an app mounted as a sub-application
    # has neither and is not something this table classifies.
    if hasattr(route, "path") and not hasattr(route, "methods"):
        return [WEBSOCKET]
    return []


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

CREDENTIALS_DDL = """
CREATE TABLE IF NOT EXISTS user_credentials (
    username    TEXT PRIMARY KEY,
    token_sha   TEXT NOT NULL,
    created_at  REAL NOT NULL,
    revoked_at  REAL
);
"""


def digest(token: str) -> str:
    """The stored form. A credential this process is handed is never written
    down in the form it arrived in, so a reader of the table cannot present
    what it finds there."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def init_credentials(db: aiosqlite.Connection) -> None:
    await db.executescript(CREDENTIALS_DDL)
    await db.commit()


async def put_credential(
    db: aiosqlite.Connection, username: str, token: str
) -> dict:
    """Mint or replace one user's credential. Replacing clears a revocation:
    the operator issuing a new value has not asked for it to arrive dead."""
    name = normalize_username(username)
    if not name:
        raise ValueError("a credential names the user it belongs to")
    if not token:
        raise ValueError("a credential needs a value")
    now = time.time()
    await db.execute(
        "INSERT INTO user_credentials (username, token_sha, created_at, revoked_at) "
        "VALUES (?, ?, ?, NULL) "
        "ON CONFLICT(username) DO UPDATE SET "
        "token_sha=excluded.token_sha, created_at=excluded.created_at, revoked_at=NULL",
        (name, digest(token), now),
    )
    await db.commit()
    return {"username": name, "created_at": now}


async def revoke_credential(db: aiosqlite.Connection, username: str) -> bool:
    """Mark a credential dead. The row is kept rather than deleted so the
    console can say "revoked" where it would otherwise have to say "never
    had one", which are different sentences with different remedies."""
    name = normalize_username(username)
    cursor = await db.execute(
        "UPDATE user_credentials SET revoked_at = ? "
        "WHERE username = ? AND revoked_at IS NULL",
        (time.time(), name),
    )
    await db.commit()
    return cursor.rowcount > 0


async def user_for_token(db: aiosqlite.Connection, token: str) -> str | None:
    """The user a presented credential belongs to, or None.

    Read per call, never cached: requirement is that a revocation lands on the
    holder's next request without a restart, and a cache is exactly the thing
    that would make that false while every test of the table itself stayed
    green.
    """
    if not token:
        return None
    rows = await db.execute(
        "SELECT username, token_sha FROM user_credentials WHERE revoked_at IS NULL"
    )
    presented = digest(token)
    for username, token_sha in await rows.fetchall():
        # Constant-time, on bytes. `compare_digest` raises TypeError on a str
        # holding anything outside ASCII, and on a websocket handshake that
        # 500 reaches the browser as "this gateway has no such route".
        if hmac.compare_digest(presented.encode("ascii"), str(token_sha).encode("ascii")):
            return username
    return None


def normalize_username(name: object) -> str:
    return str(name or "").strip().lower()


def primary_user() -> str:
    """The user the shared `COMMS_BEARER_TOKEN` stands for, or empty.

    Empty is the pre-scoping hive: the shared token reaches everything, as it
    always has. See the module docstring — this is the switch, and its default
    is "behave as before".
    """
    return normalize_username(os.environ.get("COMMS_PRIMARY_USER", ""))


# ---------------------------------------------------------------------------
# The caller
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Caller:
    """Who comms decided is asking."""

    username: str = ""
    is_admin: bool = False
    #: True when the caller reaches every conversation: the admin token, or
    #: the shared token on a hive that has named no primary user.
    unscoped: bool = False

    @property
    def recognised(self) -> bool:
        return bool(self.username) or self.is_admin or self.unscoped

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

    Order matters. The admin token is checked first because the console and
    the operator must keep a way into a wedged conversation whatever else is
    configured. The shared token is checked before the credentials table so
    that a hive naming no primary user keeps its present behaviour even if
    somebody has already minted rows.
    """
    admin = os.environ.get("COMMS_ADMIN_BEARER_TOKEN", "")
    service = os.environ.get("COMMS_BEARER_TOKEN", "")
    if not token:
        # Bypass mode: an unset service token has always meant "no gating", so
        # a fresh container can boot before its secrets are wired. Keeping
        # that here is deliberate — changing it would make a token-less deploy
        # refuse every route, and this module is not the place to decide that.
        if not service:
            return Caller(unscoped=True)
        return Caller()
    if admin and hmac.compare_digest(token.encode("utf-8"), admin.encode("utf-8")):
        return Caller(is_admin=True, unscoped=True)
    if service and hmac.compare_digest(token.encode("utf-8"), service.encode("utf-8")):
        named = primary_user()
        return Caller(username=named) if named else Caller(unscoped=True)
    named = await user_for_token(db, token)
    return Caller(username=named) if named else Caller()


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
    rows = await db.execute(
        "SELECT owner FROM minds WHERE mind_id = ? OR name = ?", (key, key.lower())
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
    return await owner_of_mind(db, mind_id) == caller.username


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
            keys.add(str(mind_id))
            keys.add(normalize_username(name))
    return keys


def filter_sessions(
    sessions: Iterable[dict], caller: Caller, keys: set[str]
) -> list[dict]:
    if caller.sees_everything():
        return list(sessions)
    out = []
    for session in sessions:
        mind = session.get("mind_id")
        if str(mind or "") in keys or normalize_username(mind) in keys:
            out.append(session)
    return out


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
            if not caller.is_admin:
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

        for route in scope["app"].router.routes:
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
