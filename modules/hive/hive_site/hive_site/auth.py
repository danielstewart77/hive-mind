"""Accounts and sign-in for the hive console and the terminal site.

One module installed into both images, not copied into each. The two sites
share one account store and one sign-in.

The hive signs people in itself. Nothing else is involved: no other program,
no other service.

A person proves who they are with a password (or, where it is set up,
Microsoft or Windows sign-in). The site then makes a fresh random key for
that sign-in and puts it in the browser as an HttpOnly cookie; the cookie
*is* the sign-in. Only the one-way scramble of the key is written, to the
hive's one record of keys, under the person's name with an end time twelve
hours after the sign-in (`core.record_sign_in`). Nothing is written when the
key is used and there is no limit for sitting idle: the key stops working
twelve hours after it was made, at the gateway, the memory store, the tools
and here, because all of them read the same record on the request.

Signing out ends that one key. A person can end every sign-in of their own,
an administrator can end anyone's, and turning an account off, changing a
password or resetting one ends all of that person's. A cookie from before this
scheme is no key the record knows, so its holder signs in again.

Admin is membership of the `admins` group and nothing else, read afresh on
every request. Users are never deleted. The last enabled admin cannot be
disabled or leave `admins`.

The accounts live in the hive's one Postgres, through `store`, and the
console and the terminal share them there.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Any, Protocol

from fastapi import HTTPException, Request
from starlette.requests import HTTPConnection

_bcrypt: Any = None
try:
    import bcrypt

    _bcrypt = bcrypt
except ImportError:  # pragma: no cover - exercised through fallback path
    pass

from itsdangerous import BadSignature, BadTimeSignature, URLSafeTimedSerializer  # the Microsoft flow cookie

from . import store as data_store

SESSION_COOKIE_NAME = "hive_session"
#: The cookie that carries a person's sign-in key.
#: How long a sign-in lasts: twelve hours from the moment of signing in.
SIGN_IN_SECONDS = 12 * 60 * 60
#: The cookie's own life matches the key's. The record is what decides.
SESSION_MAX_AGE = SIGN_IN_SECONDS
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
    message = "Signing in is not possible just now. Try again in a moment."


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
    async def end_all_sign_ins(self, username: str) -> int: ...


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


def new_key() -> str:
    """A fresh random key for one sign-in. Never stored: only its scramble."""
    return secrets.token_urlsafe(32)


def key_digest(key: str) -> str:
    """The one-way scramble the record holds for a key (the same one the
    gateway, the memory store and the tools look a presented key up by)."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _clock() -> float:
    from hive_data import core

    return core.now()


async def begin_sign_in(username: str) -> str:
    """Make the key for one sign-in and write its scramble to the record, with
    its end twelve hours from now. Returns the key, for the browser.

    When the record cannot be written nobody is left holding a key nothing
    accepts: the sign-in fails, with a plain sentence.
    """
    key = new_key()
    try:
        await data_store.record_sign_in(
            username=normalize_username(username), token_sha=key_digest(key),
            ends_at=_clock() + SIGN_IN_SECONDS)
    except Exception as exc:  # noqa: BLE001 - any failure to write is one sentence
        raise ServiceUnavailable(
            "Signing in did not work because the hive could not record your "
            "sign-in. Nothing was changed; try again in a moment.") from exc
    return key


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
        return await self._view_of(row), await begin_sign_in(row["username"])

    async def user_for_token(self, token: str) -> dict | None:
        """The person a key is for, read from the record on this request: the
        key must be in it, not ended and not past its time. Nothing is
        remembered from an earlier yes and nothing is written for a use."""
        if not token:
            return None
        sign_in = await data_store.sign_in_for_digest(key_digest(token))
        if sign_in is None:
            return None
        row = await data_store.get_user(normalize_username(sign_in["username"]))
        if row is None or row["disabled_at"] is not None:
            return None
        return await self._view_of(row)

    async def sign_out(self, token: str) -> None:
        """End this one key. Other devices stay signed in."""
        if token:
            await data_store.end_sign_in(key_digest(token))

    async def end_all_sign_ins(self, username: str) -> int:
        """End every sign-in this person holds, on every device."""
        return await data_store.end_sign_ins(normalize_username(username))

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
            # Every sign-in of this person ends, this device's too; this
            # device is then given a fresh one so the person is not thrown out.
            await data_store.end_sign_ins(row["username"])
            return await begin_sign_in(row["username"])

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
                await data_store.end_sign_ins(row["username"])
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
            await data_store.end_sign_ins(row["username"])
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

    Told apart by class rather than by message, and asked here rather than
    at each call site: a username, an email and a domain account are each
    unique, and the console answers 409 for all three.
    """
    from hive_data.errors import Conflict as DataConflict

    return isinstance(exc, DataConflict)


# ── settings left over from the authentication service ──────────────────

_OLD_SETTINGS = ("AUTH_SERVICE_URL", "AUTH_SERVICE_SITE", "HIVE_AUTH_BACKEND")

OLD_SETTINGS_SENTENCE = (
    "The hive now signs people in itself and no longer uses a separate "
    "authentication service: remove {names} from this site's settings and "
    "start it again.")


class OldSettings(RuntimeError):
    """Settings that still name the separate authentication service."""


def refuse_old_settings() -> None:
    """Stop the site starting half-working. An install whose settings still
    name the authentication service is told, in one plain sentence, what to
    change; it is not started."""
    left = [name for name in _OLD_SETTINGS if os.getenv(name, "").strip()]
    if left:
        raise OldSettings(OLD_SETTINGS_SENTENCE.format(names=", ".join(left)))


# ── which store ─────────────────────────────────────────────────────────


def get_store() -> Store:
    """The one store this install signs in against: the hive's own."""
    return BuiltinStore()


# ── requests ────────────────────────────────────────────────────────────


def session_token(conn: HTTPConnection, store: Store | None = None) -> str:
    return conn.cookies.get((store or get_store()).cookie_name) or ""


async def resolve_user(conn: HTTPConnection) -> dict | None:
    """The signed-in user behind a request, verified once per request against
    the record of keys.

    Raises ServiceUnavailable when the store cannot answer.
    """
    state = conn.scope.setdefault("state", {})
    if _STATE_KEY in state:
        return state[_STATE_KEY]
    store = get_store()
    user = await store.user_for_token(session_token(conn, store))
    state[_STATE_KEY] = user
    return user


# ── Microsoft and Windows sessions ──────────────────────────────────────


async def _flow_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(await secret_key(), salt="hive-sso-flow")


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


def request_is_https(request: HTTPConnection) -> bool:
    """Whether the browser reached this site over https.

    A TLS-terminating reverse proxy hands the site plain http and says what
    the browser used in `X-Forwarded-Proto`, so that header decides when it
    is present. A cookie marked Secure on a plain-http answer is dropped by
    the browser at any address but localhost, and sign-in by IP address
    then loops back to the login page without a word.
    """
    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded:
        return forwarded.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


def _cookie_kwargs(request: HTTPConnection) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "max_age": SESSION_MAX_AGE,
        "httponly": True,
        "samesite": "lax",
        "secure": request_is_https(request),
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


def set_session_cookie(request: HTTPConnection, response, token: str) -> None:
    """The browser holds the key, and only here: where scripts and other
    sites cannot read it."""
    response.set_cookie(get_store().cookie_name, token,
                        **_cookie_kwargs(request))


def clear_session_cookie(response) -> None:
    _delete_cookie(response, get_store().cookie_name)
    # The cookie an older sign-in left behind is cleared along with it.
    _delete_cookie(response, "hive_sso_session")


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
    """A fresh sign-in key for an existing account, recorded as a real
    sign-in is. For seeding and tests."""
    return await begin_sign_in(user["username"])


async def seed_first_user() -> None:
    """Create the initial admin from the environment on an empty store.

    Idempotent: does nothing once any user row exists, so the seed
    credential matters only for the very first launch and is inert
    afterward.

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
