"""Authentication for the hive admin site.

The hive owns its auth outright — no external auth service — so a cloned
hive is self-contained. One sqlite db (`HIVE_DB_PATH`) holds the users;
the terminal site shares this same db and `HIVE_SECRET_KEY`, so a session
issued by either site verifies on both.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from config import BASE_DIR, SECRET_KEY

try:
    import bcrypt as _bcrypt
except ImportError:  # pragma: no cover - exercised through fallback path
    _bcrypt = None

try:
    from itsdangerous import BadSignature, BadTimeSignature, URLSafeTimedSerializer
except ImportError:  # pragma: no cover - exercised through fallback path
    BadSignature = BadTimeSignature = Exception
    URLSafeTimedSerializer = None


SESSION_COOKIE_NAME = "hive_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30


def _db_path() -> Path:
    return Path(os.getenv("HIVE_DB_PATH", str(BASE_DIR.parent / "data" / "hive.db")))


def _secret_key() -> str:
    return os.getenv("HIVE_SECRET_KEY", SECRET_KEY)


def _cookie_domain() -> str:
    """Parent domain shared with the terminal site; empty means host-only."""
    return os.getenv("HIVE_COOKIE_DOMAIN", "")


def init_auth_db(db_path: Path | None = None) -> Path:
    """Create the auth database and users table if missing."""
    path = Path(db_path or _db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                disabled_at INTEGER
            )
            """
        )
        conn.commit()
    finally:
        conn.close()
    return path


def seed_first_user() -> None:
    """Create the initial account from env on an empty database.

    Idempotent: does nothing once any user row exists, so the seed
    credential matters only for the very first launch and is inert
    afterward.
    """
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


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = init_auth_db(db_path)
    conn = sqlite3.connect(path)
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
    return "pbkdf2$200000$%s$%s" % (
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


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


def create_user(
    username: str,
    password: str,
    *,
    is_admin: bool = False,
    db_path: Path | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Create a hive user."""
    normalized = username.strip().lower()
    if not normalized:
        raise ValueError("username must not be empty")

    password_hash = hash_password(password)
    now = int(time.time())
    conn = _connect(db_path)
    try:
        if replace:
            conn.execute(
                """
                INSERT INTO users (username, password_hash, is_admin, created_at, disabled_at)
                VALUES (?, ?, ?, ?, NULL)
                ON CONFLICT(username) DO UPDATE SET
                    password_hash = excluded.password_hash,
                    is_admin = excluded.is_admin,
                    disabled_at = NULL
                """,
                (normalized, password_hash, int(is_admin), now),
            )
        else:
            conn.execute(
                """
                INSERT INTO users (username, password_hash, is_admin, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (normalized, password_hash, int(is_admin), now),
            )
        conn.commit()
    finally:
        conn.close()
    return get_user_by_username(normalized, db_path=db_path)


def get_user_by_username(username: str, *, db_path: Path | None = None) -> dict[str, Any] | None:
    normalized = username.strip().lower()
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, username, password_hash, is_admin, created_at, disabled_at FROM users WHERE username = ?",
            (normalized,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id: int, *, db_path: Path | None = None) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, username, password_hash, is_admin, created_at, disabled_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def verify_user_credentials(username: str, password: str) -> dict[str, Any] | None:
    user = get_user_by_username(username)
    if not user or user.get("disabled_at"):
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user


def _make_serializer():
    if URLSafeTimedSerializer is None:
        return None
    return URLSafeTimedSerializer(_secret_key(), salt="hive-session")


def create_session_token(user: dict[str, Any]) -> str:
    payload = {"user_id": user["id"], "username": user["username"]}
    serializer = _make_serializer()
    if serializer is not None:
        return serializer.dumps(payload)

    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body_b64 = base64.urlsafe_b64encode(body).decode("ascii")
    signature = hmac.new(
        _secret_key().encode("utf-8"),
        body_b64.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{body_b64}.{signature}"


def read_session_token(token: str) -> dict[str, Any] | None:
    serializer = _make_serializer()
    if serializer is not None:
        try:
            return serializer.loads(token, max_age=SESSION_MAX_AGE)
        except (BadSignature, BadTimeSignature):
            return None

    try:
        body_b64, signature = token.split(".", 1)
    except ValueError:
        return None

    expected = hmac.new(
        _secret_key().encode("utf-8"),
        body_b64.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None

    try:
        body = base64.urlsafe_b64decode(body_b64.encode("ascii"))
        return json.loads(body.decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None


def get_current_user_from_request(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    session = read_session_token(token)
    if not session:
        return None
    user = get_user_by_id(int(session["user_id"]))
    if not user or user.get("disabled_at"):
        return None
    return user


def require_auth(request: Request) -> dict[str, Any]:
    user = get_current_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def set_session_cookie(response, user: dict[str, Any]) -> None:
    kwargs: dict[str, Any] = {
        "max_age": SESSION_MAX_AGE,
        "httponly": True,
        "samesite": "lax",
        "secure": True,
    }
    domain = _cookie_domain()
    if domain:
        kwargs["domain"] = domain
    response.set_cookie(SESSION_COOKIE_NAME, create_session_token(user), **kwargs)


def clear_session_cookie(response) -> None:
    domain = _cookie_domain()
    if domain:
        response.delete_cookie(SESSION_COOKIE_NAME, domain=domain)
    else:
        response.delete_cookie(SESSION_COOKIE_NAME)
