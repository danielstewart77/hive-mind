"""Who may reach which route: one table, enforced in front of every route.

Every route is classified in a single table. The classes are:

- `public` — no sign-in (the sign-in page itself, health, static files, and
  routes that check a bearer of their own).
- `self` — any signed-in user, including one who must still change their
  password (the change-password page).
- `user` — any signed-in user; the handler filters what it lists to what
  the user owns (`visible_minds`, `visible_sessions`).
- `owner` — a route about one mind, named by a path parameter. The table
  declares which parameter and how it resolves (a mind's name or id, a
  session id, an operation's request id) to the owning user. An admin
  passes; anyone else passes only as that owner. An owner that cannot be
  resolved — comms down, an unknown mind, a mind with no owner — refuses.
- `admin` — admins only.
- `bearer` — reached only with the bearer the rule names (a mind
  publishing); no session admits it.

Any rule may also name a bearer (`Rule(..., bearer="design")`): a request
presenting one is admitted by that bearer and nothing else. The middleware
verifies it against the table's token before the route runs — scheme
compared without regard to case, value in constant time — and a wrong one
is refused, never answered from the cookie beside it. A route reads what was
verified with `bearer_of(request)`; it never reads the header itself. A
request carrying more than one Authorization header is refused outright,
since two readers of one request could otherwise each believe a different
one.

A long-lived connection — a websocket, a streamed response — is decided
again every `RECHECK_SECONDS` for as long as it stays open, so a disable, a
reset or an owner moved ends it rather than waiting for its next request.
A refused websocket is accepted and then closed with its reason's code
(4401, 4403, 4428), because a browser never sees the code of a socket closed
before it was accepted.

A route missing from the table is `admin`. That default is what makes a
page added later safe without its author doing anything; websocket routes
are classified the same way.

The middleware is pure ASGI so websockets pass through it too, and it
decides before the route runs, so a handler with no authorization code of
its own is still covered. This module knows nothing of the app around it:
the console and the terminal each hand it their own table and resolvers.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import os
import urllib.parse
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field

import httpx
from starlette.requests import HTTPConnection
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Match, Mount
from starlette.websockets import WebSocket

import auth

PUBLIC = "public"
SELF = "self"
USER = "user"
OWNER = "owner"
ADMIN = "admin"
BEARER = "bearer"
_KINDS = {PUBLIC, SELF, USER, OWNER, ADMIN, BEARER}

# Set on every refusal this module makes, naming why ("admin" or "owner"),
# so a refusal here can never be mistaken for one a route made itself.
REFUSAL_HEADER = "x-hive-refused"
# How a refused websocket is closed, by reason: no session, a password that
# must be changed first, or a route this user may not reach.
CLOSE_UNAUTHENTICATED = 4401
CLOSE_MUST_CHANGE_PASSWORD = 4428
CLOSE_REFUSED = 4403
PASSWORD_PATH = "/password"
LOGIN_PATH = "/login"
# How often a connection that stays open is decided again.
RECHECK_SECONDS = 30.0
# Where the middleware records the bearer it verified for a request.
_BEARER_STATE_KEY = "hive_verified_bearer"


@dataclass(frozen=True)
class Rule:
    kind: str
    # For `owner`: the path parameter naming the mind, operation or session.
    param: str | None = None
    # The bearer (a name in the table's `bearers`) that also admits this
    # route; the middleware verifies it before the route runs.
    bearer: str | None = None
    # A `user` rule only for these parameter values; any other is `admin`.
    only_when: tuple[tuple[str, frozenset[str]], ...] = ()

    def __post_init__(self):
        if self.kind not in _KINDS:
            raise ValueError(f"unknown route class {self.kind!r}")
        if self.kind == OWNER and not self.param:
            raise ValueError("an owner rule names the parameter it resolves")
        if self.kind == BEARER and not self.bearer:
            raise ValueError("a bearer rule names the bearer it takes")


DEFAULT_RULE = Rule(ADMIN)
MOUNT = "MOUNT"
WEBSOCKET = "WS"

# A resolver turns a parameter's value into the owning username, or None.
Resolver = Callable[[str], Awaitable[str | None]]


@dataclass(frozen=True)
class BearerToken:
    """A credential a mind presents in place of a session.

    `token` reads the expected value at request time; empty means none is
    configured, answered with `unset_status` (503 where the route cannot
    work without one, 401 where it simply admits no bearer)."""

    token: Callable[[], str]
    unset_status: int = 401
    unset_message: str = "Authentication required"


@dataclass
class RouteTable:
    rules: dict[tuple[str, str], Rule] = field(default_factory=dict)
    resolvers: dict[str, Resolver] = field(default_factory=dict)
    bearers: dict[str, BearerToken] = field(default_factory=dict)

    def add(self, rule: Rule, *routes: str) -> None:
        """Classify routes written as "METHOD /path/{param}" ("WS" for a
        websocket, "MOUNT" for a mounted app)."""
        for spec in routes:
            method, _, path = spec.partition(" ")
            key = (method.upper(), path)
            if key in self.rules:
                raise ValueError(f"{spec} is classified twice")
            if rule.kind == OWNER and rule.param not in self.resolvers:
                raise ValueError(f"no resolver for {rule.param!r}")
            if rule.bearer and rule.bearer not in self.bearers:
                raise ValueError(f"no token for bearer {rule.bearer!r}")
            self.rules[key] = rule

    def rule_for(self, method: str, path: str) -> Rule:
        method = "GET" if method.upper() == "HEAD" else method.upper()
        return self.rules.get((method, path), DEFAULT_RULE)

    def unlisted(self, app) -> list[tuple[str, str]]:
        """Every route the app serves that this table does not name."""
        missing = []
        for route in app.router.routes:
            for method in _route_methods(route):
                if (method, route.path) not in self.rules:
                    missing.append((method, route.path))
        return missing


def _route_methods(route: BaseRoute) -> list[str]:
    if isinstance(route, Mount):
        return [MOUNT]
    methods = getattr(route, "methods", None)
    if not methods:
        return [WEBSOCKET]
    return sorted(m for m in methods if m != "HEAD")


# ── names and owners ────────────────────────────────────────────────────


def normalize_mind_name(name: object) -> str:
    """The one place a mind's name is put in comparable form."""
    return str(name or "").strip().lower()


def _comms_url() -> str:
    return (os.getenv("GATEWAY_API_URL") or "http://hive-comms:8424").rstrip("/")


def _comms_headers() -> dict[str, str]:
    token = os.getenv("COMMS_BEARER_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _comms_get(path: str) -> object | None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{_comms_url()}{path}", headers=_comms_headers())
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except ValueError:
        return None


async def registered_minds() -> list[dict] | None:
    """Comms' registry, or None when it cannot be read."""
    rows = await _comms_get("/broker/minds")
    if not isinstance(rows, list):
        return None
    return [row for row in rows if isinstance(row, dict)]


def find_mind(minds: Iterable[dict], key: str) -> dict | None:
    """A mind by its id, or failing that by its name."""
    minds = list(minds)
    for mind in minds:
        if str(mind.get("id") or "") == key:
            return mind
    wanted = normalize_mind_name(key)
    for mind in minds:
        if normalize_mind_name(mind.get("name")) == wanted:
            return mind
    return None


def owner_of(mind: dict | None) -> str | None:
    """A mind's owner, or None when it has none (a mind registered before
    owners existed, or by a comms that does not report them)."""
    owner = auth.normalize_username(str((mind or {}).get("owner") or ""))
    return owner or None


async def owner_of_mind(key: str) -> str | None:
    minds = await registered_minds()
    if minds is None:
        return None
    return owner_of(find_mind(minds, key))


async def owner_of_session(session_id: str) -> str | None:
    quoted = urllib.parse.quote(session_id, safe="")
    session = await _comms_get(f"/sessions/{quoted}")
    if not isinstance(session, dict):
        return None
    mind_id = str(session.get("mind_id") or "")
    return await owner_of_mind(mind_id) if mind_id else None


def owns(user: dict | None, mind: dict) -> bool:
    if not user:
        return False
    return bool(user.get("is_admin")) or owner_of(mind) == user.get("username")


def visible_minds(user: dict | None, minds: list[dict]) -> list[dict]:
    """The minds a listing shows this user: all of them to an admin."""
    if user and user.get("is_admin"):
        return list(minds)
    return [mind for mind in minds if owns(user, mind)]


def visible_sessions(
    user: dict | None, sessions: list[dict], minds: list[dict]
) -> list[dict]:
    """Sessions of the minds this user may see; `minds` must carry owners."""
    if user and user.get("is_admin"):
        return list(sessions)
    keys: set[str] = set()
    for mind in visible_minds(user, minds):
        keys.add(str(mind.get("id") or ""))
        keys.add(normalize_mind_name(mind.get("name")))
    keys.discard("")
    return [
        session
        for session in sessions
        if isinstance(session, dict)
        and (
            str(session.get("mind_id") or "") in keys
            or normalize_mind_name(session.get("mind_id")) in keys
        )
    ]


# ── the middleware ──────────────────────────────────────────────────────


def bearer_of(conn: HTTPConnection) -> str | None:
    """The bearer the middleware verified for this request, by its table
    name, or None when the request was admitted by a session."""
    return (conn.scope.get("state") or {}).get(_BEARER_STATE_KEY)


def _authorization_headers(scope) -> list[bytes]:
    return [value for name, value in scope.get("headers") or [] if name.lower() == b"authorization"]


def _presented_bearer(header: bytes) -> bytes | None:
    scheme, _, value = header.strip().partition(b" ")
    if scheme.lower() != b"bearer":
        return None
    return value.strip()


def _wants_page(route: BaseRoute, method: str) -> bool:
    response_class = getattr(route, "response_class", None)
    return method == "GET" and isinstance(response_class, type) and issubclass(
        response_class, HTMLResponse
    )


def _refusal(why: str, page: bool) -> Response:
    message = (
        "This needs an admin." if why == ADMIN else "That mind is not yours."
    )
    headers = {REFUSAL_HEADER: why}
    if page:
        return HTMLResponse(f"<h1>Not allowed</h1><p>{message}</p>", 403, headers=headers)
    return JSONResponse({"detail": message}, 403, headers=headers)


def _unavailable(message: str, page: bool) -> Response:
    if page:
        return HTMLResponse(f"<h1>{message}</h1><p>Try again shortly.</p>", 503)
    return JSONResponse({"detail": message}, 503)


def _close_code(refusal: Response) -> int:
    if refusal.status_code == 401:
        return CLOSE_UNAUTHENTICATED
    if refusal.status_code == 303 and refusal.headers.get("location") == PASSWORD_PATH:
        return CLOSE_MUST_CHANGE_PASSWORD
    return CLOSE_REFUSED


async def _close_socket(scope, receive, send, code: int) -> None:
    """Accept, then close with `code`: a socket closed before it is accepted
    reaches a browser as a bare 1006, whatever code the server meant."""
    websocket = WebSocket(scope, receive, send)
    await websocket.accept()
    await websocket.close(code=code)


class AccessMiddleware:
    def __init__(self, app, table: RouteTable):
        self.app = app
        self.table = table

    def _match(self, scope) -> tuple[BaseRoute | None, dict]:
        router_app = scope.get("app")
        routes = getattr(getattr(router_app, "router", None), "routes", None) or []
        for route in routes:
            match, child = route.matches(scope)
            if match == Match.FULL:
                return route, dict(child.get("path_params") or {})
        return None, {}

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        route, params = self._match(scope)
        if route is None:
            await self.app(scope, receive, send)
            return
        is_socket = scope["type"] == "websocket"
        method = MOUNT if isinstance(route, Mount) else (
            WEBSOCKET if is_socket else scope["method"]
        )
        rule = self.table.rule_for(method, getattr(route, "path", ""))
        page = not is_socket and _wants_page(route, method)

        authorization = _authorization_headers(scope)
        if len(authorization) > 1:
            refusal: Response | None = JSONResponse(
                {"detail": "More than one Authorization header."}, 400
            )
            await self._refuse(scope, receive, send, refusal, is_socket)
            return
        presented = _presented_bearer(authorization[0]) if authorization else None
        if rule.bearer and presented is not None:
            refusal = self._verify_bearer(rule.bearer, presented)
            if refusal is not None:
                await self._refuse(scope, receive, send, refusal, is_socket)
                return
            scope.setdefault("state", {})[_BEARER_STATE_KEY] = rule.bearer
            await self.app(scope, receive, send)
            return
        if rule.kind == BEARER:
            refusal = JSONResponse({"detail": "Authentication required"}, 401)
            await self._refuse(scope, receive, send, refusal, is_socket)
            return
        if rule.kind == PUBLIC:
            await self.app(scope, receive, send)
            return

        refusal = await self._decide(scope, rule, params, page)
        if refusal is not None:
            await self._refuse(scope, receive, send, refusal, is_socket)
            return
        await self._run_watched(scope, receive, send, rule, params, is_socket)

    def _verify_bearer(self, name: str, presented: bytes) -> Response | None:
        bearer = self.table.bearers[name]
        expected = bearer.token() or ""
        if not expected:
            return JSONResponse({"detail": bearer.unset_message}, bearer.unset_status)
        if not hmac.compare_digest(presented, expected.encode("utf-8", "surrogatepass")):
            return JSONResponse({"detail": "Authentication required"}, 401)
        return None

    async def _refuse(self, scope, receive, send, refusal: Response, is_socket: bool) -> None:
        if is_socket:
            await _close_socket(scope, receive, send, _close_code(refusal))
        else:
            await refusal(scope, receive, send)

    async def _run_watched(self, scope, receive, send, rule, params, is_socket) -> None:
        """Run the route, deciding again every RECHECK_SECONDS while it is
        still running: an open socket is closed, a streaming response is
        ended. A request that finishes first is never touched."""
        state = {"open": False, "done": False}

        async def watched_send(message):
            kind = message["type"]
            if state["done"]:
                return
            if kind in ("websocket.accept", "http.response.start"):
                state["open"] = True
            elif kind == "websocket.close" or (
                kind == "http.response.body" and not message.get("more_body", False)
            ):
                state["done"] = True
            await send(message)

        app_task = asyncio.ensure_future(self.app(scope, receive, watched_send))
        try:
            await self._watch(app_task, state, send, scope, rule, params, is_socket)
        finally:
            if not app_task.done():
                app_task.cancel()

    async def _watch(self, app_task, state, send, scope, rule, params, is_socket) -> None:
        while True:
            done, _ = await asyncio.wait({app_task}, timeout=RECHECK_SECONDS)
            if done:
                app_task.result()
                return
            if not state["open"] or state["done"]:
                continue
            fresh = dict(scope, state={})
            try:
                refusal = await self._decide(fresh, rule, params, page=False)
            except Exception:  # noqa: BLE001 - a failed recheck is not a refusal
                refusal = None
            if refusal is None or refusal.status_code == 503:
                continue
            state["done"] = True
            app_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await app_task
            if is_socket:
                await send({"type": "websocket.close", "code": _close_code(refusal), "reason": ""})
            else:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            return

    async def _decide(self, scope, rule: Rule, params: dict, page: bool) -> Response | None:
        conn = HTTPConnection(scope)
        try:
            user = await auth.resolve_user(conn)
        except auth.ServiceUnavailable as exc:
            return _unavailable(exc.message, page)
        if user is None:
            if page:
                target = urllib.parse.quote(conn.url.path, safe="/")
                return RedirectResponse(f"{LOGIN_PATH}?next={target}", 303)
            return JSONResponse({"detail": "Authentication required"}, 401)
        if user.get("must_change") and rule.kind != SELF:
            return RedirectResponse(PASSWORD_PATH, 303)
        kind = rule.kind
        if kind == USER and rule.only_when:
            matched = all(
                str(params.get(name, "")) in allowed for name, allowed in rule.only_when
            )
            kind = USER if matched else ADMIN
        if user.get("is_admin") or kind in (SELF, USER):
            return None
        if kind == ADMIN:
            return _refusal(ADMIN, page)
        param = rule.param or ""
        owner = await self.table.resolvers[param](str(params.get(param, "")))
        if owner is None or owner != user.get("username"):
            return _refusal(OWNER, page)
        return None
