"""Tests for session-based authentication (management UI)."""

from __future__ import annotations


import pytest
from tests.scratch import RawSQL


def test_hash_password_returns_prefixed_string() -> None:
    from auth_session import hash_password

    result = hash_password("test123")
    assert result.startswith("bcrypt$") or result.startswith("pbkdf2$")


def test_hash_password_empty_raises() -> None:
    from auth_session import hash_password

    with pytest.raises(ValueError):
        hash_password("")


def test_verify_password_correct() -> None:
    from auth_session import hash_password, verify_password

    hashed = hash_password("mypassword")
    assert verify_password("mypassword", hashed) is True


def test_verify_password_wrong() -> None:
    from auth_session import hash_password, verify_password

    hashed = hash_password("mypassword")
    assert verify_password("wrong", hashed) is False


async def test_create_user_and_get_by_username(db_conn: RawSQL) -> None:
    from auth_session import create_user, get_user_by_username

    user = await create_user("daniel", "pass123")
    assert user["username"] == "daniel"

    fetched = await get_user_by_username("daniel")
    assert fetched is not None
    assert fetched["username"] == "daniel"


async def test_create_user_duplicate_raises(db_conn: RawSQL) -> None:
    """Named by what the application branches on, not by one backend's
    exception class. The bootstrap seed catches "this user already exists" to
    survive an overlapping redeploy, and which class carries it depends on
    which store answered - `sqlite3.IntegrityError` on the file,
    `hive_data.Conflict` on Postgres. A test naming the first asserts
    something the two halves behaving alike contradicts."""
    from auth_session import create_user
    from server import _already_seeded

    await create_user("daniel", "pass123")
    with pytest.raises(Exception) as caught:
        await create_user("daniel", "pass456")
    assert _already_seeded(caught.value)


def test_create_session_token_and_read_back() -> None:
    from auth_session import create_session_token, read_session_token

    user = {"id": 1, "username": "daniel"}
    token = create_session_token(user)
    payload = read_session_token(token)
    assert payload is not None
    assert payload["user_id"] == 1


def test_read_session_token_invalid_returns_none() -> None:
    from auth_session import read_session_token

    result = read_session_token("garbage.token.value")
    assert result is None


async def test_verify_user_credentials_valid(db_conn: RawSQL) -> None:
    from auth_session import create_user, verify_user_credentials

    await create_user("daniel", "pass123")
    user = await verify_user_credentials("daniel", "pass123")
    assert user is not None
    assert user["username"] == "daniel"


async def test_verify_user_credentials_disabled_returns_none(
    db_conn: RawSQL,
) -> None:
    import time

    from auth_session import create_user, verify_user_credentials

    await create_user("daniel", "pass123")
    db_conn.execute(
        "UPDATE users SET disabled_at = ? WHERE username = ?",
        (int(time.time()), "daniel"),
    )
    db_conn.commit()

    result = await verify_user_credentials("daniel", "pass123")
    assert result is None


