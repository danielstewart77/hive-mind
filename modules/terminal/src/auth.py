"""Accounts and sessions for the hive console — and, copied verbatim, the
terminal site.

One interface, two backings, chosen by configuration:

- `builtin` — the console's own sqlite db (`HIVE_DB_PATH`). A cloned hive
  signs in with no other service. Sessions are signed cookies carrying the
  user's session version, so a password change, reset or disable ends every
  other live session at once.
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

This file imports nothing from the app around it, so the terminal site runs
the identical module against the identical db.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
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

SESSION_COOKIE_NAME = "hive_session"
AUTH_SERVICE_COOKIE_NAME = "auth_session"
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

    def sign_in(self, username: str, password: str) -> tuple[dict, str]: ...
    def user_for_token(self, token: str) -> dict | None: ...
    def sign_out(self, token: str) -> None: ...
    def change_password(self, token: str, current: str, new: str) -> str: ...
    def list_users(self) -> list[dict]: ...
    def get_user(self, username: str) -> dict: ...
    def create_user(
        self,
        username: str,
        *,
        email: str | None = None,
        domain_account: str | None = None,
        temp_password: str | None = None,
    ) -> dict: ...
    def update_user(
        self, username: str, *, email: str | None = None, disabled: bool | None = None
    ) -> dict: ...
    def reset_password(self, username: str, temp_password: str) -> dict: ...
    def list_groups(self) -> list[dict]: ...
    def create_group(self, name: str) -> dict: ...
    def add_member(self, group: str, username: str) -> None: ...
    def remove_member(self, group: str, username: str) -> None: ...


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


# ── built-in: hive.db ───────────────────────────────────────────────────


def _db_path() -> Path:
    default = Path(__file__).resolve().parent.parent / "data" / "hive.db"
    return Path(os.getenv("HIVE_DB_PATH", str(default)))


def _cookie_domain() -> str:
    """Parent domain shared with the terminal site; empty means host-only."""
    return os.getenv("HIVE_COOKIE_DOMAIN", "")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    disabled_at INTEGER
);
CREATE TABLE IF NOT EXISTS groups (
    name TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS group_members (
    group_name TEXT NOT NULL REFERENCES groups(name),
    user_id INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (group_name, user_id)
);
CREATE TABLE IF NOT EXISTS auth_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_ADDED_COLUMNS = {
    "must_change": "INTEGER NOT NULL DEFAULT 0",
    "email": "TEXT",
    "domain_account": "TEXT",
    "session_version": "INTEGER NOT NULL DEFAULT 0",
}

_migrated: set[str] = set()
_migrate_lock = threading.Lock()


def init_auth_db(db_path: Path | None = None) -> Path:
    """Create the account tables, and bring an older db forward.

    An older db held admin as a column, `is_admin`. Every account it marked
    joins the `admins` group and the column is dropped, in one transaction,
    so no reader ever sees an admin who is neither.
    """
    path = Path(db_path or _db_path())
    key = str(path.resolve())
    if key in _migrated and path.exists():
        return path
    with _migrate_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
            for column, ddl in _ADDED_COLUMNS.items():
                if column not in columns:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {column} {ddl}")
            conn.execute(
                "INSERT OR IGNORE INTO groups (name, created_at) VALUES (?, ?)",
                (ADMINS, int(time.time())),
            )
            if "is_admin" in columns:
                conn.execute(
                    "INSERT OR IGNORE INTO group_members (group_name, user_id) "
                    "SELECT ?, id FROM users WHERE is_admin = 1",
                    (ADMINS,),
                )
                conn.execute("ALTER TABLE users DROP COLUMN is_admin")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS users_email_unique "
                "ON users(lower(email)) WHERE email IS NOT NULL AND email != ''"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS users_domain_account_unique "
                "ON users(lower(domain_account)) "
                "WHERE domain_account IS NOT NULL AND domain_account != ''"
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        _migrated.add(key)
    return path


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = init_auth_db(db_path)
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


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


def secret_key(db_path: Path | None = None) -> str:
    """What signs built-in session cookies.

    `HIVE_SECRET_KEY` when configured. Otherwise a random key generated on
    first use and kept in the db — the terminal shares the db, so it shares
    the key — never a constant shipped in the source.
    """
    configured = os.getenv("HIVE_SECRET_KEY", "").strip()
    if configured and configured != _SHIPPED_SECRET:
        return configured
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO auth_settings (key, value) VALUES ('secret_key', ?)",
            (secrets.token_urlsafe(48),),
        )
        return conn.execute(
            "SELECT value FROM auth_settings WHERE key = 'secret_key'"
        ).fetchone()[0]
    finally:
        conn.close()


def _serializer(db_path: Path | None = None) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key(db_path), salt="hive-session")


def read_session_token(token: str) -> dict[str, Any] | None:
    try:
        payload = _serializer().loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, BadTimeSignature):
        return None
    return payload if isinstance(payload, dict) else None


class BuiltinStore:
    """Accounts in hive.db; sessions are signed cookies with a version."""

    cookie_name = SESSION_COOKIE_NAME

    def __init__(self, db_path: Path | None = None):
        self._db_path = db_path

    # -- rows --------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        return _connect(self._db_path)

    def _groups_of(self, conn: sqlite3.Connection, user_id: int) -> list[str]:
        return [
            row[0]
            for row in conn.execute(
                "SELECT group_name FROM group_members WHERE user_id = ?", (user_id,)
            )
        ]

    def _view(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
        return _view(
            user_id=row["id"],
            username=row["username"],
            email=row["email"],
            domain_account=row["domain_account"],
            must_change=bool(row["must_change"]),
            disabled=row["disabled_at"] is not None,
            groups=self._groups_of(conn, row["id"]),
        )

    def _row(self, conn: sqlite3.Connection, username: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (normalize_username(username),)
        ).fetchone()
        if row is None:
            raise NotFound("No such user.")
        return row

    def _token(self, row: sqlite3.Row) -> str:
        return _serializer(self._db_path).dumps(
            {
                "user_id": row["id"],
                "username": row["username"],
                "v": int(row["session_version"]),
            }
        )

    @staticmethod
    def _enabled_admins_besides(conn: sqlite3.Connection, user_id: int) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM users u JOIN group_members m ON m.user_id = u.id "
            "WHERE m.group_name = ? AND u.disabled_at IS NULL AND u.id != ?",
            (ADMINS, user_id),
        ).fetchone()[0]

    def _guard_last_admin(self, conn: sqlite3.Connection, row: sqlite3.Row) -> None:
        """Refuse a change that would leave no enabled admin. Runs inside
        the caller's IMMEDIATE transaction, so two admins disabling each
        other at once cannot both pass the count."""
        is_enabled_admin = row["disabled_at"] is None and ADMINS in self._groups_of(
            conn, row["id"]
        )
        if is_enabled_admin and self._enabled_admins_besides(conn, row["id"]) == 0:
            raise Conflict("The hive must keep at least one enabled admin.")

    # -- sessions ----------------------------------------------------

    def sign_in(self, username: str, password: str) -> tuple[dict, str]:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ?", (normalize_username(username),)
            ).fetchone()
            stored = row["password_hash"] if row is not None else ""
            matched = verify_password(password, stored or _miss_hash())
            if row is None or not stored or not matched:
                raise SignInRefused()
            if row["disabled_at"] is not None:
                raise AccountDisabled()
            return self._view(conn, row), self._token(row)
        finally:
            conn.close()

    def user_for_token(self, token: str) -> dict | None:
        payload = read_session_token(token) if token else None
        if not payload:
            return None
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (int(payload.get("user_id") or 0),)
            ).fetchone()
            if row is None or row["disabled_at"] is not None:
                return None
            if int(payload.get("v") or 0) != int(row["session_version"]):
                return None
            return self._view(conn, row)
        finally:
            conn.close()

    def sign_out(self, token: str) -> None:
        # A signed cookie is ended by clearing it; the version is left alone
        # so the user's other devices stay signed in.
        del token

    def change_password(self, token: str, current: str, new: str) -> str:
        user = self.user_for_token(token)
        if user is None:
            raise SignInRefused("Sign in again to change your password.")
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, user["username"])
            if not verify_password(current, row["password_hash"]):
                raise WrongPassword()
            _check_new_password(current, new)
            conn.execute(
                "UPDATE users SET password_hash = ?, must_change = 0, "
                "session_version = session_version + 1 WHERE id = ?",
                (hash_password(new), row["id"]),
            )
            conn.execute("COMMIT")
            return self._token(self._row(conn, user["username"]))
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # -- users -------------------------------------------------------

    def list_users(self) -> list[dict]:
        conn = self._conn()
        try:
            rows = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
            return [self._view(conn, row) for row in rows]
        finally:
            conn.close()

    def get_user(self, username: str) -> dict:
        conn = self._conn()
        try:
            return self._view(conn, self._row(conn, username))
        finally:
            conn.close()

    def find_user(
        self, *, email: str | None = None, domain_account: str | None = None
    ) -> dict | None:
        """The account an outside identity names, case-insensitively."""
        column, value = ("email", email) if email else ("domain_account", domain_account)
        if not value:
            return None
        conn = self._conn()
        try:
            row = conn.execute(
                f"SELECT * FROM users WHERE lower({column}) = lower(?)", (value.strip(),)
            ).fetchone()
            return self._view(conn, row) if row is not None else None
        finally:
            conn.close()

    def create_user(
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
        conn = self._conn()
        try:
            try:
                conn.execute(
                    "INSERT INTO users (username, password_hash, created_at, must_change, "
                    "email, domain_account) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        name,
                        hash_password(temp_password) if temp_password else "",
                        int(time.time()),
                        1 if temp_password else 0,
                        (email or "").strip() or None,
                        (domain_account or "").strip() or None,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(
                    "A user with that username, email or domain account already exists."
                ) from exc
            return self._view(conn, self._row(conn, name))
        finally:
            conn.close()

    def update_user(
        self, username: str, *, email: str | None = None, disabled: bool | None = None
    ) -> dict:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, username)
            if email is not None:
                try:
                    conn.execute(
                        "UPDATE users SET email = ? WHERE id = ?",
                        (email.strip() or None, row["id"]),
                    )
                except sqlite3.IntegrityError as exc:
                    raise Conflict("Another user already has that email.") from exc
            if disabled is True and row["disabled_at"] is None:
                self._guard_last_admin(conn, row)
                conn.execute(
                    "UPDATE users SET disabled_at = ?, session_version = session_version + 1 "
                    "WHERE id = ?",
                    (int(time.time()), row["id"]),
                )
            elif disabled is False:
                conn.execute("UPDATE users SET disabled_at = NULL WHERE id = ?", (row["id"],))
            conn.execute("COMMIT")
            return self._view(conn, self._row(conn, username))
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def reset_password(self, username: str, temp_password: str) -> dict:
        if not temp_password:
            raise Invalid("A temporary password is required.")
        conn = self._conn()
        try:
            row = self._row(conn, username)
            conn.execute(
                "UPDATE users SET password_hash = ?, must_change = 1, "
                "session_version = session_version + 1 WHERE id = ?",
                (hash_password(temp_password), row["id"]),
            )
            return self._view(conn, self._row(conn, username))
        finally:
            conn.close()

    # -- groups ------------------------------------------------------

    def list_groups(self) -> list[dict]:
        conn = self._conn()
        try:
            groups = []
            for (name,) in conn.execute("SELECT name FROM groups ORDER BY name"):
                members = [
                    r[0]
                    for r in conn.execute(
                        "SELECT u.username FROM group_members m JOIN users u "
                        "ON u.id = m.user_id WHERE m.group_name = ? ORDER BY u.username",
                        (name,),
                    )
                ]
                groups.append({"name": name, "members": members})
            return groups
        finally:
            conn.close()

    def create_group(self, name: str) -> dict:
        group = normalize_username(name)
        if not group:
            raise Invalid("A group name is required.")
        conn = self._conn()
        try:
            try:
                conn.execute(
                    "INSERT INTO groups (name, created_at) VALUES (?, ?)",
                    (group, int(time.time())),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("That group already exists.") from exc
            return {"name": group, "members": []}
        finally:
            conn.close()

    def _group(self, conn: sqlite3.Connection, name: str) -> str:
        group = normalize_username(name)
        if conn.execute("SELECT 1 FROM groups WHERE name = ?", (group,)).fetchone() is None:
            raise NotFound("No such group.")
        return group

    def add_member(self, group: str, username: str) -> None:
        # Membership is read on every request, so a change of group lands on
        # the member's next request without ending their session.
        conn = self._conn()
        try:
            name = self._group(conn, group)
            row = self._row(conn, username)
            conn.execute(
                "INSERT OR IGNORE INTO group_members (group_name, user_id) VALUES (?, ?)",
                (name, row["id"]),
            )
        finally:
            conn.close()

    def remove_member(self, group: str, username: str) -> None:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            name = self._group(conn, group)
            row = self._row(conn, username)
            if name == ADMINS:
                self._guard_last_admin(conn, row)
            conn.execute(
                "DELETE FROM group_members WHERE group_name = ? AND user_id = ?",
                (name, row["id"]),
            )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


# ── auth-service ────────────────────────────────────────────────────────


class AuthServiceStore:
    """The sparktobloom sign-in, spoken to over its HTTP contract."""

    cookie_name = AUTH_SERVICE_COOKIE_NAME

    def __init__(self, url: str, admin_token: str, site: str = "hive", timeout: float = 5.0):
        self._url = url.rstrip("/")
        self._admin_token = admin_token
        self._site = site
        self._client = httpx.Client(timeout=timeout)

    def _call(
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
            response = self._client.request(
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

    def _admin(self, method: str, path: str, json_body: dict | None = None) -> httpx.Response:
        response = self._call(method, path, admin=True, json_body=json_body)
        if response.status_code == 404:
            raise NotFound(self._detail(response) or None)
        if response.status_code == 409:
            raise Conflict(self._detail(response) or None)
        if response.status_code >= 400:
            raise Invalid(self._detail(response) or "Refused by the sign-in service.")
        return response

    # -- sessions ----------------------------------------------------

    def sign_in(self, username: str, password: str) -> tuple[dict, str]:
        response = self._call(
            "POST", "/login", json_body={"username": username, "password": password}
        )
        if response.status_code == 403:
            raise AccountDisabled()
        if response.status_code != 200:
            raise SignInRefused()
        token = str(response.json().get("token") or "")
        verified = self._call("GET", "/verify", bearer=token, params={"site": self._site})
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

    def user_for_token(self, token: str) -> dict | None:
        if not token:
            return None
        response = self._call("GET", "/verify", bearer=token, params={"site": self._site})
        if response.status_code != 200:
            return None
        return self._verified_view(response.json())

    def sign_out(self, token: str) -> None:
        if token:
            self._call("POST", "/logout", bearer=token)

    def change_password(self, token: str, current: str, new: str) -> str:
        _check_new_password(current, new)
        response = self._call(
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

    def list_users(self) -> list[dict]:
        return [self._admin_view(r) for r in self._admin("GET", "/admin/users").json()]

    def get_user(self, username: str) -> dict:
        return self._admin_view(
            self._admin("GET", f"/admin/users/{normalize_username(username)}").json()
        )

    def create_user(
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
        body: dict[str, Any] = {"username": name}
        if email:
            body["email"] = email.strip()
        if temp_password is not None:
            body["temp_password"] = temp_password
        self._admin("POST", "/admin/users", body)
        # A user not granted the hive cannot reach it; the console only
        # creates users for itself.
        self._admin("PUT", f"/admin/users/{name}/sites/{self._site}")
        return self.get_user(name)

    def update_user(
        self, username: str, *, email: str | None = None, disabled: bool | None = None
    ) -> dict:
        body: dict[str, Any] = {}
        if email is not None:
            body["email"] = email
        if disabled is not None:
            body["disabled"] = disabled
        return self._admin_view(
            self._admin("PATCH", f"/admin/users/{normalize_username(username)}", body).json()
        )

    def reset_password(self, username: str, temp_password: str) -> dict:
        if not temp_password:
            raise Invalid("A temporary password is required.")
        return self._admin_view(
            self._admin(
                "POST",
                f"/admin/users/{normalize_username(username)}/reset",
                {"temp_password": temp_password},
            ).json()
        )

    def list_groups(self) -> list[dict]:
        return [
            {"name": g.get("name"), "members": sorted(g.get("members") or [])}
            for g in self._admin("GET", "/admin/groups").json()
        ]

    def create_group(self, name: str) -> dict:
        group = normalize_username(name)
        if not group:
            raise Invalid("A group name is required.")
        self._admin("POST", "/admin/groups", {"name": group})
        return {"name": group, "members": []}

    def add_member(self, group: str, username: str) -> None:
        self._admin(
            "PUT",
            f"/admin/groups/{normalize_username(group)}/members/{normalize_username(username)}",
        )

    def remove_member(self, group: str, username: str) -> None:
        self._admin(
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


def resolve_user(conn: HTTPConnection) -> dict | None:
    """The signed-in user behind a request, verified once per request.

    Raises ServiceUnavailable when the store cannot answer.
    """
    state = conn.scope.setdefault("state", {})
    if _STATE_KEY in state:
        return state[_STATE_KEY]
    store = get_store()
    user = store.user_for_token(session_token(conn, store))
    state[_STATE_KEY] = user
    return user


def get_current_user_from_request(request: HTTPConnection) -> dict[str, Any] | None:
    """The signed-in user, or None — including when the store is down,
    which the access middleware has already answered with a 503 on every
    route that needs a user."""
    try:
        return resolve_user(request)
    except ServiceUnavailable:
        return None


def require_auth(request: Request) -> dict[str, Any]:
    user = get_current_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def set_session_cookie(response, token: str) -> None:
    kwargs: dict[str, Any] = {
        "max_age": SESSION_MAX_AGE,
        "httponly": True,
        "samesite": "lax",
        "secure": True,
    }
    domain = _cookie_domain()
    if domain:
        kwargs["domain"] = domain
    response.set_cookie(get_store().cookie_name, token, **kwargs)


def clear_session_cookie(response) -> None:
    domain = _cookie_domain()
    name = get_store().cookie_name
    if domain:
        response.delete_cookie(name, domain=domain)
    else:
        response.delete_cookie(name)


# ── built-in helpers for seeding, tooling and tests ─────────────────────


def create_user(
    username: str,
    password: str,
    *,
    is_admin: bool = False,
    db_path: Path | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    """A built-in account with a settled password (no change required).

    `is_admin` puts the account in `admins`; `replace` resets an existing
    account's password and re-enables it.
    """
    normalized = normalize_username(username)
    if not normalized:
        raise ValueError("username must not be empty")
    password_hash = hash_password(password)
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if replace:
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash, "
                "disabled_at = NULL, must_change = 0",
                (normalized, password_hash, int(time.time())),
            )
        else:
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (normalized, password_hash, int(time.time())),
            )
        if is_admin:
            conn.execute(
                "INSERT OR IGNORE INTO group_members (group_name, user_id) "
                "SELECT ?, id FROM users WHERE username = ?",
                (ADMINS, normalized),
            )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return BuiltinStore(db_path).get_user(normalized)


def create_session_token(user: dict[str, Any], db_path: Path | None = None) -> str:
    """A built-in session cookie value for an existing account."""
    store = BuiltinStore(db_path)
    conn = store._conn()
    try:
        return store._token(store._row(conn, user["username"]))
    finally:
        conn.close()


def seed_first_user() -> None:
    """Create the initial admin from env on an empty built-in database.

    Idempotent: does nothing once any user row exists, so the seed
    credential matters only for the very first launch and is inert
    afterward. Not used with auth-service, which seeds its own.
    """
    if backend_name() != "builtin":
        return
    conn = _connect()
    try:
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()
    if count > 0:
        return
    username = os.getenv("HIVE_SEED_USERNAME", "")
    password = os.getenv("HIVE_SEED_PASSWORD", "")
    if not username or not password:
        return
    create_user(username, password, is_admin=True)
