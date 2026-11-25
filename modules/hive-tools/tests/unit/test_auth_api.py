"""Tests for bearer token auth functions."""

from __future__ import annotations

import hashlib
import time
from tests.scratch import RawSQL


async def test_generate_token_returns_raw_and_stores_hash(db_conn: RawSQL) -> None:
    from auth_api import generate_token

    raw = await generate_token("ada")
    assert isinstance(raw, str)
    assert len(raw) > 20

    row = db_conn.execute("SELECT token_hash FROM api_tokens WHERE name = ?", ("ada",)).fetchone()
    assert row is not None
    assert row["token_hash"] == hashlib.sha256(raw.encode()).hexdigest()


async def test_generate_token_hash_is_sha256(db_conn: RawSQL) -> None:
    from auth_api import generate_token

    raw = await generate_token("bob")
    expected_hash = hashlib.sha256(raw.encode()).hexdigest()

    row = db_conn.execute("SELECT token_hash FROM api_tokens WHERE name = ?", ("bob",)).fetchone()
    assert row["token_hash"] == expected_hash


async def test_validate_token_returns_mind_name(db_conn: RawSQL) -> None:
    from auth_api import generate_token, validate_token

    raw = await generate_token("ada")
    result = await validate_token(raw)
    assert result == "ada"


async def test_validate_token_invalid_returns_none(db_conn: RawSQL) -> None:
    from auth_api import validate_token

    result = await validate_token("not-a-real-token")
    assert result is None


async def test_validate_token_revoked_returns_none(db_conn: RawSQL) -> None:
    from auth_api import generate_token, revoke_token, validate_token

    raw = await generate_token("ada")
    row = db_conn.execute("SELECT id FROM api_tokens WHERE name = ?", ("ada",)).fetchone()
    await revoke_token(row["id"])

    result = await validate_token(raw)
    assert result is None


async def test_validate_token_updates_last_used_at(db_conn: RawSQL) -> None:
    from auth_api import generate_token, validate_token

    raw = await generate_token("ada")

    row_before = db_conn.execute(
        "SELECT last_used_at FROM api_tokens WHERE name = ?", ("ada",)
    ).fetchone()
    assert row_before["last_used_at"] is None

    await validate_token(raw)

    row_after = db_conn.execute(
        "SELECT last_used_at FROM api_tokens WHERE name = ?", ("ada",)
    ).fetchone()
    assert row_after["last_used_at"] is not None
    assert row_after["last_used_at"] >= int(time.time()) - 5


async def test_revoke_token_sets_revoked_at(db_conn: RawSQL) -> None:
    from auth_api import generate_token, revoke_token

    await generate_token("ada")
    row = db_conn.execute("SELECT id FROM api_tokens WHERE name = ?", ("ada",)).fetchone()

    result = await revoke_token(row["id"])
    assert result is True

    row_after = db_conn.execute(
        "SELECT revoked_at FROM api_tokens WHERE id = ?", (row["id"],)
    ).fetchone()
    assert row_after["revoked_at"] is not None


async def test_revoking_an_already_revoked_token_answers_false(
    db_conn: RawSQL,
) -> None:
    """The count is the arbitration. A caller told it succeeded twice cannot
    tell a revocation it performed from one it merely repeated - and the
    operator's page says the token is gone either way, which is the sentence
    that matters when the guard is missing and the bearer keeps working."""
    from auth_api import generate_token, revoke_token

    await generate_token("ada")
    token_id = db_conn.execute(
        "SELECT id FROM api_tokens WHERE name = ?", ("ada",)).fetchone()["id"]

    assert await revoke_token(token_id) is True
    first = db_conn.execute(
        "SELECT revoked_at FROM api_tokens WHERE id = ?", (token_id,)
    ).fetchone()["revoked_at"]

    assert await revoke_token(token_id) is False
    # And the second attempt did not move the moment it was revoked at.
    assert db_conn.execute(
        "SELECT revoked_at FROM api_tokens WHERE id = ?", (token_id,)
    ).fetchone()["revoked_at"] == first


async def test_list_tokens_returns_all(db_conn: RawSQL) -> None:
    from auth_api import generate_token, list_tokens, revoke_token

    await generate_token("ada")
    await generate_token("bob")

    # Revoke one
    row = db_conn.execute("SELECT id FROM api_tokens WHERE name = ?", ("bob",)).fetchone()
    await revoke_token(row["id"])

    tokens = await list_tokens()
    assert len(tokens) == 2
    names = {t["name"] for t in tokens}
    assert names == {"ada", "bob"}
