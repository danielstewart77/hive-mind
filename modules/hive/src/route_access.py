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

A route missing from the table is `admin`. That default is what makes a
page added later safe without its author doing anything; websocket routes
are classified the same way.

The middleware is pure ASGI so websockets pass through it too, and it
decides before the route runs, so a handler with no authorization code of
its own is still covered. This module knows nothing of the app around it:
the console and the terminal each hand it their own table and resolvers.
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field

import httpx
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Match, Mount
from starlette.websockets import WebSocketClose

import auth

PUBLIC = "public"
SELF = "self"
USER = "user"
OWNER = "owner"
ADMIN = "admin"
_KINDS = {PUBLIC, SELF, USER, OWNER, ADMIN}

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


@dataclass(frozen=True)
class Rule:
    kind: str
    # For `owner`: the path parameter naming the mind, operation or session.
    param: str | None = None
    # The route checks an `Authorization: Bearer` credential itself (a mind
    # publishing); a request carrying one is handed straight to the route.
    bearer: bool = False
    # A `user` rule only for these parameter values; any other is `admin`.
    only_when: tuple[tuple[str, frozenset[str]], ...] = ()

    def __post_init__(self):
        if self.kind not in _KINDS:
            raise ValueError(f"unknown route class {self.kind!r}")
        if self.kind == OWNER and not self.param:
            raise ValueError("an owner rule names the parameter it resolves")


DEFAULT_RULE = Rule(ADMIN)
MOUNT = "MOUNT"
WEBSOCKET = "WS"

# A resolver turns a parameter's value into the owning username, or None.
Resolver = Callable[[str], Awaitable[str | None]]


@dataclass
class RouteTable:
    rules: dict[tuple[str, str], Rule] = field(default_factory=dict)
    resolvers: dict[str, Resolver] = field(default_factory=dict)

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
        if rule.kind == PUBLIC:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        if rule.bearer and headers.get(b"authorization", b"").lower().startswith(b"bearer "):
            await self.app(scope, receive, send)
            return

        page = not is_socket and _wants_page(route, method)
        refusal = await self._decide(scope, rule, params, page)
        if refusal is None:
            await self.app(scope, receive, send)
        elif is_socket:
            await WebSocketClose(code=_close_code(refusal))(scope, receive, send)
        else:
            await refusal(scope, receive, send)

    async def _decide(self, scope, rule: Rule, params: dict, page: bool) -> Response | None:
        from starlette.requests import HTTPConnection

        conn = HTTPConnection(scope)
        try:
            user = await run_in_threadpool(auth.resolve_user, conn)
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
