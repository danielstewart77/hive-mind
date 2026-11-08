"""Accounts and sessions for the hive console and the terminal site.

One module installed into both images, not copied into each. It was a copy
for a year and the copies drifted: the terminal's still opened `hive.db`
with `sqlite3` by hand and minted its own signing key into that file, so a
password change made in the console moved `session_version` in one store
while the terminal read the old value out of the other, and the cookie both
sites share was honoured by one and refused by the other — which presents as
being signed out of the terminal and nowhere else.

One interface, two backings, chosen by configuration:

- `builtin` — the console's own account rows, reached through `store`:
  the hive's Postgres or this host's own file, whichever that module is
  pointed at. No SQL lives here. A cloned hive
  signs in with no other service. Sessions are signed cookies carrying the
  user's session version, so a password change, reset or disable ends every
  other live session at once. A group change does not touch it: membership,
  and with it admin, is read afresh on every request, so it lands on the
  member's next request without signing them out.
- `auth-service` — the sparktobloom sign-in (`AUTH_SERVICE_URL`). The
  session is auth-service's own `auth_session` token, verified on every
  request with `/verify?site=hive` and never cached, so a disable lands on
  the very next request. Admin operations go to its `/admin` API behind
  `AUTH_SERVICE_ADMIN_TOKEN`. When it cannot be reached the console says
  "sign-in service unavailable"; it never falls back to hive.db.

`HIVE_AUTH_BACKEND` names the backing; left unset, auth-service is used
exactly when `AUTH_SERVICE_URL` is set. One backing is active per install,
and each reads only its own cookie, so a `hive_session` cookie minted before
the switch to auth-service is not a session.

Admin is membership of the `admins` group and nothing else. Users are never
deleted. The last enabled admin cannot be disabled or leave `admins`.

The accounts live wherever `store` is pointed — the hive's one Postgres or
this host's own file. The terminal site ran a verbatim copy of this module
against the same file and cannot any longer: this one is async and reads a
store the terminal does not have. Moving it is its own story, and until it
runs the two sites share accounts only on an install where both are pointed
at the same store.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import time
import weakref
from typing import Any, Protocol

import httpx
from fastapi import HTTPException, Request
from starlette.requests import HTTPConnection

_bcrypt: Any = None
try:
    import bcrypt

    _bcrypt = bcrypt
except ImportError:  # pragma: no cover - exercised through fallback path
    pass

from itsdangerous import BadSignature, BadTimeSignature, URLSafeTimedSerializer

from . import store as data_store

SESSION_COOKIE_NAME = "hive_session"
AUTH_SERVICE_COOKIE_NAME = "auth_session"
# A session begun by Microsoft or Windows sign-in (see sso.py). Its own
# cookie on either store: it is a console session and nothing else, never an
# auth-service token another site would accept.
SSO_COOKIE_NAME = "hive_sso_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30
ADMINS = "admins"
# The default this project once shipped. A key equal to it is no key.
_SHIPPED_SECRET = "change-this-in-production"  # secret-guard: allow
# Where a request's resolved user is kept, so one request verifies once.
_STATE_KEY = "hive_auth_user"

# Further ways to sign in, offered on the sign-in page beside the password
# form: each is {"label": ..., "href": ...}. Microsoft and Windows sign-in
# register themselves here when configured.
SIGN_IN_METHODS: list[dict[str, str]] = []


# ── errors ──────────────────────────────────────────────────────────────


class AuthError(Exception):
    status = 400
    message = "Request refused."

    def __init__(self, message: str | None = None):
        super().__init__(message or self.message)
        self.message = message or self.message


class SignInRefused(AuthError):
    status = 401
    message = "Invalid username or password."


class AccountDisabled(AuthError):
    status = 403
    message = "This account is disabled."


class NotGranted(AuthError):
    status = 403
    message = "This account has not been set up for the hive."


class NotSetUp(AuthError):
    status = 403
    message = "Your account has not been set up. Ask the hive's admin to create it."


class WrongPassword(AuthError):
    status = 403
    message = "The current password is wrong."


class ServiceUnavailable(AuthError):
    status = 503
    message = "Sign-in service unavailable."


class NotFound(AuthError):
    status = 404
    message = "No such user or group."


class Conflict(AuthError):
    status = 409
    message = "That conflicts with an existing record."


class Invalid(AuthError):
    status = 400


# ── the interface ───────────────────────────────────────────────────────


class Store(Protocol):
    """What the console and the terminal need of an account store.

    A user is {id, username, email, domain_account, must_change, disabled,
    groups, is_admin}; usernames are lowercase and are the owner key comms
    holds for every mind.
    """

    cookie_name: str

    async def sign_in(self, username: str,
                      password: str) -> tuple[dict, str]: ...
    async def user_for_token(self, token: str) -> dict | None: ...
    async def sign_out(self, token: str) -> None: ...
    async def change_password(self, token: str, current: str,
                              new: str) -> str: ...
    async def list_users(self) -> list[dict]: ...
    async def get_user(self, username: str) -> dict: ...
    async def create_user(
        self,
        username: str,
        *,
        email: str | None = None,
        domain_account: str | None = None,
        temp_password: str | None = None,
    ) -> dict: ...
    async def update_user(
        self, username: str, *, email: str | None = None,
        disabled: bool | None = None
    ) -> dict: ...
    async def reset_password(self, username: str,
                             temp_password: str) -> dict: ...
    async def list_groups(self) -> list[dict]: ...
    async def create_group(self, name: str) -> dict: ...
    async def add_member(self, group: str, username: str) -> None: ...
    async def remove_member(self, group: str, username: str) -> None: ...
    async def find_user(
        self, *, email: str | None = None, domain_account: str | None = None
    ) -> dict | None: ...
    async def sso_version(self, username: str) -> int: ...
    async def sso_user(self, username: str,
                       version: int) -> dict | None: ...


def normalize_username(username: str) -> str:
    return str(username or "").strip().lower()


def _view(
    *,
    user_id: Any,
    username: str,
    email: str | None,
    domain_account: str | None,
    must_change: bool,
    disabled: bool,
    groups: list[str],
) -> dict:
    groups = sorted({str(g).lower() for g in groups})
    return {
        "id": user_id,
        "username": username,
        "email": email or None,
        "domain_account": domain_account or None,
        "must_change": bool(must_change),
        "disabled": bool(disabled),
        "groups": groups,
        "is_admin": ADMINS in groups,
    }


def _check_new_password(current: str, new: str) -> None:
    if len(new or "") < 8:
        raise Invalid("The new password must be at least 8 characters.")
    if new == current:
        raise Invalid("The new password must differ from the current one.")


# ── built-in ────────────────────────────────────────────────────────────


def _cookie_domain() -> str:
    """Parent domain shared with the terminal site; empty means host-only."""
    return os.getenv("HIVE_COOKIE_DOMAIN", "")


def hash_password(password: str) -> str:
    """Hash a password with bcrypt when available, otherwise PBKDF2."""
    if not password:
        raise ValueError("password must not be empty")

    password_bytes = password.encode("utf-8")
    if _bcrypt is not None:
        hashed = _bcrypt.hashpw(password_bytes, _bcrypt.gensalt())
        return f"bcrypt${hashed.decode('utf-8')}"

    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password_bytes, salt, 200_000)
    salt_encoded = base64.urlsafe_b64encode(salt).decode("ascii")
    digest_encoded = base64.urlsafe_b64encode(digest).decode("ascii")
    return f"pbkdf2$200000${salt_encoded}${digest_encoded}"


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against a stored hash."""
    if not password_hash:
        return False

    password_bytes = password.encode("utf-8")
    if password_hash.startswith("bcrypt$") and _bcrypt is not None:
        stored = password_hash.split("$", 1)[1].encode("utf-8")
        return _bcrypt.checkpw(password_bytes, stored)

    if password_hash.startswith("pbkdf2$"):
        _, rounds, salt_b64, digest_b64 = password_hash.split("$", 3)
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password_bytes, salt, int(rounds))
        return hmac.compare_digest(actual, expected)

    return False


_MISS_HASH: str | None = None


def _miss_hash() -> str:
    """Compared against on an unknown name, so a miss costs what a hit does."""
    global _MISS_HASH
    if _MISS_HASH is None:
        _MISS_HASH = hash_password(secrets.token_urlsafe(16))
    return _MISS_HASH


_SECRET: str | None = None


async def secret_key() -> str:
    """What signs built-in session cookies.

    `HIVE_SECRET_KEY` when configured. Otherwise a key generated on first use
    and kept in the store — the terminal site reads the same rows, so it
    reads the same key — never a constant shipped in the source.

    Written and read in one statement rather than a read followed by a write.
    Two workers reaching an empty install at once would otherwise each mint a
    key and each write it; whichever landed second would sign every later
    cookie and invalidate the ones the other had already issued.

    Held in memory once read. The key never changes for the life of an
    install, and this is reached on every request that carries a cookie.
    """
    global _SECRET
    configured = os.getenv("HIVE_SECRET_KEY", "").strip()
    if configured and configured != _SHIPPED_SECRET:
        return configured
    if _SECRET is None:
        _SECRET = await data_store.put_auth_setting_if_absent(
            "secret_key", secrets.token_urlsafe(48))
    return _SECRET


def forget_secret_key() -> None:
    """Drop the remembered key. For a test that moves to another store."""
    global _SECRET
    _SECRET = None


async def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(await secret_key(), salt="hive-session")


async def read_session_token(token: str) -> dict[str, Any] | None:
    try:
        payload = (await _serializer()).loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, BadTimeSignature):
        return None
    return payload if isinstance(payload, dict) else None


class BuiltinStore:
    """Accounts in the console's own rows; sessions are signed cookies
    carrying a version.

    No SQL here. Every read and write goes through `store`, so the rules
    below — the last-admin guard above all — are decided once and hold
    whichever database the console is pointed at.
    """

    cookie_name = SESSION_COOKIE_NAME

    # -- rows --------------------------------------------------------

    async def _view_of(self, row: dict) -> dict:
        return _view(
            user_id=row["id"],
            username=row["username"],
            email=row["email"],
            domain_account=row["domain_account"],
            must_change=bool(row["must_change"]),
            disabled=row["disabled_at"] is not None,
            groups=await data_store.groups_of(row["id"]),
        )

    async def _row(self, username: str, *, private: bool = False) -> dict:
        row = await data_store.get_user(normalize_username(username),
                                        private=private)
        if row is None:
            raise NotFound("No such user.")
        return row

    async def _token(self, row: dict) -> str:
        return (await _serializer()).dumps({
            "user_id": row["id"],
            "username": row["username"],
            "v": int(row["session_version"]),
        })

    async def _guard_last_admin(self, row: dict) -> None:
        """Refuse a change that would leave no enabled admin.

        Runs inside the caller's unit of work and counts in the database, so
        two admins disabling each other at once cannot both pass the count —
        which they would if each listed the group and filtered a copy that
        still held the other.
        """
        if row["disabled_at"] is not None:
            return
        if ADMINS not in await data_store.groups_of(row["id"]):
            return
        remaining = await data_store.enabled_members_besides(
            group_name=ADMINS, user_id=row["id"])
        if remaining == 0:
            raise Conflict("The hive must keep at least one enabled admin.")

    # -- sessions ----------------------------------------------------

    async def sign_in(self, username: str,
                      password: str) -> tuple[dict, str]:
        row = await data_store.get_user(normalize_username(username),
                                        private=True)
        stored = row["password_hash"] if row is not None else ""
        matched = verify_password(password, stored or _miss_hash())
        if row is None or not stored or not matched:
            raise SignInRefused()
        if row["disabled_at"] is not None:
            raise AccountDisabled()
        return await self._view_of(row), await self._token(row)

    async def user_for_token(self, token: str) -> dict | None:
        payload = await read_session_token(token) if token else None
        if not payload:
            return None
        row = await data_store.get_user_by_id(
            int(payload.get("user_id") or 0))
        if row is None or row["disabled_at"] is not None:
            return None
        # The cookie names a person as well as a number, and the row has to
        # be that person. Two sites sharing a signing key but pointed at two
        # stores — which each site's own `HIVE_DATABASE_URL` permits — number
        # their accounts independently, and an id looked up alone signed a
        # non-admin in on the other site as whoever held that id there,
        # admin included.
        if normalize_username(str(payload.get("username") or "")) != \
                normalize_username(str(row["username"])):
            return None
        if int(payload.get("v") or 0) != int(row["session_version"]):
            return None
        return await self._view_of(row)

    async def sign_out(self, token: str) -> None:
        # A signed cookie is ended by clearing it; the version is left alone
        # so the user's other devices stay signed in.
        del token

    async def sso_version(self, username: str) -> int:
        return int((await self._row(username))["session_version"])

    async def sso_user(self, username: str, version: int) -> dict | None:
        """The account behind a Microsoft or Windows session. It carries the
        session version, so a disable, reset or password change ends it as it
        ends a password session. Such a session never owes a password change:
        it was not begun with one."""
        row = await data_store.get_user(normalize_username(username))
        if row is None or row["disabled_at"] is not None:
            return None
        if int(version) != int(row["session_version"]):
            return None
        return dict(await self._view_of(row), must_change=False)

    async def change_password(self, token: str, current: str,
                              new: str) -> str:
        user = await self.user_for_token(token)
        if user is None:
            raise SignInRefused("Sign in again to change your password.")
        async with data_store.transaction():
            row = await self._row(user["username"], private=True)
            if not verify_password(current, row["password_hash"]):
                raise WrongPassword()
            _check_new_password(current, new)
            await data_store.update_user(
                row["id"], password_hash=hash_password(new), must_change=0)
            await data_store.bump_session_version(row["id"])
            return await self._token(await self._row(user["username"]))

    # -- users -------------------------------------------------------

    async def list_users(self) -> list[dict]:
        return [await self._view_of(row)
                for row in await data_store.list_users()]

    async def get_user(self, username: str) -> dict:
        return await self._view_of(await self._row(username))

    async def find_user(
        self, *, email: str | None = None,
        domain_account: str | None = None
    ) -> dict | None:
        """The account an outside identity names, case-insensitively."""
        value = email if email else domain_account
        if not value or not str(value).strip():
            return None
        row = await data_store.find_user(
            email=email.strip() if email else None,
            domain_account=(domain_account.strip()
                            if not email and domain_account else None))
        return await self._view_of(row) if row is not None else None

    async def create_user(
        self,
        username: str,
        *,
        email: str | None = None,
        domain_account: str | None = None,
        temp_password: str | None = None,
    ) -> dict:
        name = normalize_username(username)
        if not name:
            raise Invalid("A username is required.")
        if temp_password is not None and not temp_password:
            raise Invalid("A temporary password must not be empty.")
        try:
            await data_store.create_user(
                username=name,
                password_hash=(hash_password(temp_password)
                               if temp_password else ""),
                created_at=int(time.time()),
                must_change=1 if temp_password else 0,
                email=(email or "").strip() or None,
                domain_account=(domain_account or "").strip() or None,
            )
        except Exception as exc:
            if _is_duplicate(exc):
                raise Conflict(
                    "A user with that username, email or domain account "
                    "already exists.") from exc
            raise
        return await self._view_of(await self._row(name))

    async def update_user(
        self, username: str, *, email: str | None = None,
        disabled: bool | None = None
    ) -> dict:
        async with data_store.transaction():
            row = await self._row(username)
            if email is not None:
                try:
                    await data_store.update_user(
                        row["id"], email=email.strip() or None)
                except Exception as exc:
                    if _is_duplicate(exc):
                        raise Conflict(
                            "Another user already has that email.") from exc
                    raise
            if disabled is True and row["disabled_at"] is None:
                await self._guard_last_admin(row)
                await data_store.update_user(row["id"],
                                             disabled_at=int(time.time()))
                await data_store.bump_session_version(row["id"])
            elif disabled is False:
                await data_store.update_user(row["id"], disabled_at=None)
            return await self._view_of(await self._row(username))

    async def reset_password(self, username: str,
                             temp_password: str) -> dict:
        if not temp_password:
            raise Invalid("A temporary password is required.")
        async with data_store.transaction():
            row = await self._row(username)
            await data_store.update_user(
                row["id"], password_hash=hash_password(temp_password),
                must_change=1)
            await data_store.bump_session_version(row["id"])
            return await self._view_of(await self._row(username))

    # -- groups ------------------------------------------------------

    async def list_groups(self) -> list[dict]:
        groups = []
        for row in await data_store.list_groups():
            name = row["name"]
            groups.append({
                "name": name,
                "members": await data_store.group_usernames(name),
            })
        return groups

    async def create_group(self, name: str) -> dict:
        group = normalize_username(name)
        if not group:
            raise Invalid("A group name is required.")
        written = await data_store.create_group(name=group,
                                                created_at=int(time.time()))
        if written == 0:
            raise Conflict("That group already exists.")
        return {"name": group, "members": []}

    async def _group(self, name: str) -> str:
        group = normalize_username(name)
        if await data_store.get_group(group) is None:
            raise NotFound("No such group.")
        return group

    async def add_member(self, group: str, username: str) -> None:
        # Membership is read on every request, so a change of group lands on
        # the member's next request without ending their session.
        name = await self._group(group)
        row = await self._row(username)
        await data_store.add_group_member(group_name=name,
                                          user_id=row["id"])

    async def remove_member(self, group: str, username: str) -> None:
        async with data_store.transaction():
            name = await self._group(group)
            row = await self._row(username)
            if name == ADMINS:
                await self._guard_last_admin(row)
            await data_store.remove_group_member(group_name=name,
                                                 user_id=row["id"])


def _is_duplicate(exc: BaseException) -> bool:
    """Whether a failed write was a uniqueness conflict.

    Told apart by class rather than by message, and asked of both stores
    here rather than at each call site: a username, an email and a domain
    account are each unique, and the console answers 409 for all three. The
    import is local so a console on the SQLite half, in an install with no
    `hive_data` on its path, still refuses a duplicate as a conflict.
    """
    import sqlite3

    if isinstance(exc, sqlite3.IntegrityError):
        return True
    try:
        from hive_data.errors import Conflict as DataConflict
    except ImportError:  # pragma: no cover - the SQLite-only install
        return False
    return isinstance(exc, DataConflict)


# ── auth-service ────────────────────────────────────────────────────────


class AuthServiceStore:
    """The sparktobloom sign-in, spoken to over its HTTP contract."""

    cookie_name = AUTH_SERVICE_COOKIE_NAME

    def __init__(self, url: str, admin_token: str, site: str = "hive",
                 timeout: float = 5.0):
        self._url = url.rstrip("/")
        self._admin_token = admin_token
        self._site = site
        self._timeout = timeout
        #: One client per event loop, not one per store and not one per call.
        #: An `httpx.AsyncClient` holds a connection pool bound to the loop
        #: that opened it: shared across loops it raises "event loop is
        #: closed" on the first reuse, and rebuilt per call it costs a TLS
        #: handshake on `/verify`, which runs on every single request. A
        #: server has one loop and so one client; the keys are weak, so a
        #: loop that goes takes its client with it.
        self._clients: weakref.WeakKeyDictionary[Any, httpx.AsyncClient] = (
            weakref.WeakKeyDictionary())

    def _loop_client(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
            self._clients[loop] = client
        return client

    async def _call(
        self,
        method: str,
        path: str,
        *,
        bearer: str | None = None,
        admin: bool = False,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        if not self._url:
            raise ServiceUnavailable()
        headers = {}
        if admin:
            if not self._admin_token:
                raise ServiceUnavailable(
                    "Sign-in service unavailable: no admin token is configured."
                )
            headers["Authorization"] = f"Bearer {self._admin_token}"
        elif bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        try:
            response = await self._loop_client().request(
                method, f"{self._url}{path}", headers=headers, json=json_body, params=params
            )
        except httpx.HTTPError as exc:
            raise ServiceUnavailable() from exc
        if response.status_code >= 500 or (admin and response.status_code == 401):
            raise ServiceUnavailable()
        return response

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        try:
            return str(response.json().get("detail") or "")
        except ValueError:
            return response.text

    def _admin_view(self, row: dict) -> dict:
        return _view(
            user_id=row.get("user_id"),
            username=row.get("username") or "",
            email=row.get("email"),
            domain_account=None,
            must_change=bool(row.get("must_change")),
            disabled=bool(row.get("disabled")),
            groups=list(row.get("groups") or []),
        )

    async def _admin(self, method: str, path: str,
                     json_body: dict | None = None) -> httpx.Response:
        response = await self._call(method, path, admin=True,
                                    json_body=json_body)
        if response.status_code == 404:
            raise NotFound(self._detail(response) or None)
        if response.status_code == 409:
            raise Conflict(self._detail(response) or None)
        if response.status_code >= 400:
            raise Invalid(self._detail(response) or "Refused by the sign-in service.")
        return response

    # -- sessions ----------------------------------------------------

    async def sign_in(self, username: str, password: str) -> tuple[dict, str]:
        response = await self._call(
            "POST", "/login", json_body={"username": username, "password": password}
        )
        if response.status_code == 403:
            raise AccountDisabled()
        if response.status_code != 200:
            raise SignInRefused()
        token = str(response.json().get("token") or "")
        verified = await self._call("GET", "/verify", bearer=token, params={"site": self._site})
        if verified.status_code == 403:
            raise NotGranted()
        if verified.status_code != 200:
            raise SignInRefused()
        return self._verified_view(verified.json()), token

    def _verified_view(self, row: dict) -> dict:
        return _view(
            user_id=row.get("user_id"),
            username=row.get("username") or "",
            email=row.get("email"),
            domain_account=None,
            must_change=bool(row.get("must_change")),
            disabled=False,
            groups=list(row.get("groups") or []),
        )

    async def user_for_token(self, token: str) -> dict | None:
        if not token:
            return None
        response = await self._call("GET", "/verify", bearer=token, params={"site": self._site})
        if response.status_code != 200:
            return None
        return self._verified_view(response.json())

    async def sign_out(self, token: str) -> None:
        if token:
            await self._call("POST", "/logout", bearer=token)

    async def _raw_user(self, username: str) -> dict | None:
        response = await self._call(
            "GET", f"/admin/users/{normalize_username(username)}", admin=True
        )
        if response.status_code != 200:
            return None
        return response.json()

    async def sso_version(self, username: str) -> int:
        del username
        return 0

    async def sso_user(self, username: str, version: int) -> dict | None:
        """The account behind a Microsoft session, read afresh from the
        admin API on every request so a disable lands at once. An account
        not granted the hive is no session here, as with /verify — which
        counts every admin as granted, so this does too."""
        del version
        row = await self._raw_user(username)
        if not row or row.get("disabled"):
            return None
        view = self._admin_view(row)
        sites = {str(s).lower() for s in row.get("sites") or []}
        if self._site.lower() not in sites and not view["is_admin"]:
            return None
        return dict(view, must_change=False)

    async def find_user(
        self, *, email: str | None = None, domain_account: str | None = None
    ) -> dict | None:
        """The account with this email. auth-service holds no domain
        accounts, so a domain account matches nobody."""
        wanted = (email or "").strip().lower()
        if domain_account or not wanted:
            return None
        for row in (await self._admin("GET", "/admin/users")).json():
            if str(row.get("email") or "").strip().lower() == wanted:
                return self._admin_view(row)
        return None

    async def change_password(self, token: str, current: str, new: str) -> str:
        _check_new_password(current, new)
        response = await self._call(
            "POST",
            "/password/change",
            bearer=token,
            json_body={"current_password": current, "new_password": new},
        )
        if response.status_code == 401:
            raise SignInRefused("Sign in again to change your password.")
        if response.status_code == 403:
            raise WrongPassword()
        if response.status_code >= 400:
            raise Invalid(self._detail(response) or None)
        return token

    # -- users and groups --------------------------------------------

    async def list_users(self) -> list[dict]:
        return [self._admin_view(r)
                for r in (await self._admin("GET",
                                            "/admin/users")).json()]

    async def get_user(self, username: str) -> dict:
        return self._admin_view(
            (await self._admin(
                "GET",
                f"/admin/users/{normalize_username(username)}")).json()
        )

    async def create_user(
        self,
        username: str,
        *,
        email: str | None = None,
        domain_account: str | None = None,
        temp_password: str | None = None,
    ) -> dict:
        if domain_account:
            raise Invalid("The sign-in service does not hold domain accounts yet.")
        name = normalize_username(username)
        if not name:
            raise Invalid("A username is required.")
        body: dict[str, Any] = {"username": name, "sites": [self._site]}
        if email:
            body["email"] = email.strip()
        if temp_password is not None:
            body["temp_password"] = temp_password
        # The grant rides in the create, so a user is never left made but
        # unable to reach the hive; a create that comes back without it is
        # not complete.
        created = (await self._admin("POST", "/admin/users", body)).json()
        if not self._granted(created):
            raise ServiceUnavailable(
                "The sign-in service created the user without granting the hive."
            )
        return self._admin_view(created)

    def _granted(self, row: object) -> bool:
        sites = row.get("sites") if isinstance(row, dict) else None
        return self._site.lower() in {str(s).lower() for s in sites or []}

    async def update_user(
        self, username: str, *, email: str | None = None,
        disabled: bool | None = None
    ) -> dict:
        body: dict[str, Any] = {}
        if email is not None:
            body["email"] = email
        if disabled is not None:
            body["disabled"] = disabled
        return self._admin_view(
            (await self._admin(
                "PATCH",
                f"/admin/users/{normalize_username(username)}",
                body)).json()
        )

    async def reset_password(self, username: str, temp_password: str) -> dict:
        if not temp_password:
            raise Invalid("A temporary password is required.")
        return self._admin_view(
            (await self._admin(
                "POST",
                f"/admin/users/{normalize_username(username)}/reset",
                {"temp_password": temp_password},
            )).json()
        )

    async def list_groups(self) -> list[dict]:
        return [
            {"name": g.get("name"), "members": sorted(g.get("members") or [])}
            for g in (await self._admin("GET", "/admin/groups")).json()
        ]

    async def create_group(self, name: str) -> dict:
        group = normalize_username(name)
        if not group:
            raise Invalid("A group name is required.")
        await self._admin("POST", "/admin/groups", {"name": group})
        return {"name": group, "members": []}

    async def add_member(self, group: str, username: str) -> None:
        await self._admin(
            "PUT",
            f"/admin/groups/{normalize_username(group)}/members/{normalize_username(username)}",
        )

    async def remove_member(self, group: str, username: str) -> None:
        await self._admin(
            "DELETE",
            f"/admin/groups/{normalize_username(group)}/members/{normalize_username(username)}",
        )


# ── which store ─────────────────────────────────────────────────────────

_auth_service_stores: dict[tuple[str, str, str], AuthServiceStore] = {}


def backend_name() -> str:
    configured = os.getenv("HIVE_AUTH_BACKEND", "").strip().lower()
    if configured in {"builtin", "auth-service"}:
        return configured
    return "auth-service" if os.getenv("AUTH_SERVICE_URL", "").strip() else "builtin"


def get_store() -> Store:
    """The one store this install signs in against."""
    if backend_name() == "builtin":
        return BuiltinStore()
    key = (
        os.getenv("AUTH_SERVICE_URL", "").strip(),
        os.getenv("AUTH_SERVICE_ADMIN_TOKEN", ""),
        os.getenv("AUTH_SERVICE_SITE", "hive").strip() or "hive",
    )
    store = _auth_service_stores.get(key)
    if store is None:
        store = AuthServiceStore(*key)
        _auth_service_stores[key] = store
    return store


# ── requests ────────────────────────────────────────────────────────────


def session_token(conn: HTTPConnection, store: Store | None = None) -> str:
    return conn.cookies.get((store or get_store()).cookie_name) or ""


async def resolve_user(conn: HTTPConnection) -> dict | None:
    """The signed-in user behind a request, verified once per request: the
    store's own session, or failing that a Microsoft or Windows one.

    Raises ServiceUnavailable when the store cannot answer.
    """
    state = conn.scope.setdefault("state", {})
    if _STATE_KEY in state:
        return state[_STATE_KEY]
    store = get_store()
    user = await store.user_for_token(session_token(conn, store))
    if user is None:
        sso_token = conn.cookies.get(SSO_COOKIE_NAME) or ""
        user = (await user_for_sso_token(store, sso_token)
                if sso_token else None)
    state[_STATE_KEY] = user
    return user


# ── Microsoft and Windows sessions ──────────────────────────────────────


async def _sso_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(await secret_key(), salt="hive-sso-session")


async def _flow_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(await secret_key(), salt="hive-sso-flow")


async def issue_sso_token(store: Store, username: str, via: str) -> str:
    return (await _sso_serializer()).dumps({
        "u": normalize_username(username),
        "v": await store.sso_version(username),
        "via": via,
    })


async def user_for_sso_token(store: Store, token: str) -> dict | None:
    try:
        payload = (await _sso_serializer()).loads(token,
                                                  max_age=SESSION_MAX_AGE)
    except (BadSignature, BadTimeSignature):
        return None
    if not isinstance(payload, dict) or not payload.get("u"):
        return None
    return await store.sso_user(str(payload["u"]),
                                int(payload.get("v") or 0))


async def sign_flow(flow: dict) -> str:
    """A sign-in in progress (state, nonce, PKCE verifier), held by the
    browser that started it."""
    return (await _flow_serializer()).dumps(flow)


async def read_flow(token: str, max_age: int = 10 * 60) -> dict | None:
    try:
        flow = (await _flow_serializer()).loads(token or "",
                                                max_age=max_age)
    except (BadSignature, BadTimeSignature):
        return None
    return flow if isinstance(flow, dict) else None


async def get_current_user_from_request(
        request: HTTPConnection) -> dict[str, Any] | None:
    """The signed-in user, or None — including when the store is down,
    which the access middleware has already answered with a 503 on every
    route that needs a user."""
    try:
        return await resolve_user(request)
    except ServiceUnavailable:
        return None


async def require_auth(request: Request) -> dict[str, Any]:
    user = await get_current_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def _cookie_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "max_age": SESSION_MAX_AGE,
        "httponly": True,
        "samesite": "lax",
        "secure": True,
    }
    domain = _cookie_domain()
    if domain:
        kwargs["domain"] = domain
    return kwargs


def _delete_cookie(response, name: str) -> None:
    domain = _cookie_domain()
    if domain:
        response.delete_cookie(name, domain=domain)
    else:
        response.delete_cookie(name)


def set_session_cookie(response, token: str) -> None:
    """A password session; any Microsoft or Windows session is ended, so the
    browser holds exactly one identity."""
    _delete_cookie(response, SSO_COOKIE_NAME)
    response.set_cookie(get_store().cookie_name, token, **_cookie_kwargs())


def set_sso_cookie(response, token: str) -> None:
    """A Microsoft or Windows session; any password session is ended."""
    _delete_cookie(response, get_store().cookie_name)
    response.set_cookie(SSO_COOKIE_NAME, token, **_cookie_kwargs())


def clear_session_cookie(response) -> None:
    _delete_cookie(response, get_store().cookie_name)
    _delete_cookie(response, SSO_COOKIE_NAME)


# ── built-in helpers for seeding, tooling and tests ─────────────────────


async def create_user(
    username: str,
    password: str,
    *,
    is_admin: bool = False,
    replace: bool = False,
) -> dict[str, Any]:
    """A built-in account with a settled password (no change required).

    `is_admin` puts the account in `admins`; `replace` resets an existing
    account's password and re-enables it. One unit of work, so an account is
    never left created and outside the group it was asked for.
    """
    normalized = normalize_username(username)
    if not normalized:
        raise ValueError("username must not be empty")
    password_hash = hash_password(password)
    async with data_store.transaction():
        existing = await data_store.get_user(normalized)
        if existing is not None:
            if not replace:
                raise Conflict("A user with that username already exists.")
            await data_store.update_user(
                existing["id"], password_hash=password_hash,
                disabled_at=None, must_change=0)
            user_id = existing["id"]
        else:
            created = await data_store.create_user(
                username=normalized, password_hash=password_hash,
                created_at=int(time.time()))
            assert created is not None  # the insert returns its row
            user_id = created["id"]
        if is_admin:
            await data_store.create_group(name=ADMINS,
                                          created_at=int(time.time()))
            await data_store.add_group_member(group_name=ADMINS,
                                              user_id=user_id)
    return await BuiltinStore().get_user(normalized)


async def create_session_token(user: dict[str, Any]) -> str:
    """A built-in session cookie value for an existing account."""
    store = BuiltinStore()
    return await store._token(await store._row(user["username"]))


async def seed_first_user() -> None:
    """Create the initial admin from the environment on an empty store.

    Idempotent: does nothing once any user row exists, so the seed
    credential matters only for the very first launch and is inert
    afterward. Not used with auth-service, which seeds its own.

    In a task of its own, and that is not a flourish. Both sites call this
    from their lifespan, which is an async generator, and `hive_data`
    refuses a unit of work opened anywhere beneath one — it walks the whole
    stack, because the hazard it guards is a generator that *yields* while
    holding a pooled connection inside an open transaction. This one does
    not: the account is written and committed before the lifespan yields at
    all. So the refusal is a false positive on a true stack, and the way
    past it is the one the refusal itself names — the write in a frame chain
    of its own. Without this, a site started against an empty Postgres dies
    in its lifespan with a transaction-misuse error, which is every first
    launch on the new store.
    """
    await asyncio.create_task(_seed_first_user())


async def _seed_first_user() -> None:
    if backend_name() != "builtin":
        return
    if await data_store.count_users() > 0:
        return
    username = os.getenv("HIVE_SEED_USERNAME", "")
    password = os.getenv("HIVE_SEED_PASSWORD", "")
    if not username or not password:
        return
    try:
        await create_user(username, password, is_admin=True)
    except Conflict:
        # The other site seeded the same empty store between the count above
        # and this write. The account exists, which is all this was for; a
        # refusal here would kill the lifespan of whichever site lost.
        return
