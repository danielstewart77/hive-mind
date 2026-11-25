"""Tests for HITL gate dependency."""

from __future__ import annotations

import time
import uuid

import pytest
from tests.scratch import RawSQL


async def test_hitl_gate_mode_off_passes_through(db_conn: RawSQL) -> None:
    from hitl import _check_hitl

    # No setting row means default "off"
    result = await _check_hitl("calendar_list_events", "ada")
    assert result == "ada"


async def test_hitl_gate_mode_on_creates_request_and_raises_202(
    db_conn: RawSQL,
) -> None:
    from hitl import HITLPendingException, _check_hitl

    db_conn.execute(
        "INSERT INTO tool_hitl_settings (tool_name, mode) VALUES (?, ?)",
        ("gmail_send_email", "on"),
    )
    db_conn.commit()

    with pytest.raises(HITLPendingException) as exc_info:
        await _check_hitl("gmail_send_email", "ada")

    assert exc_info.value.request_id is not None
    uuid.UUID(exc_info.value.request_id)  # must be valid UUID


async def test_hitl_gate_mode_sudo_with_valid_grant_passes_through(
    db_conn: RawSQL,
) -> None:
    from hitl import _check_hitl

    db_conn.execute(
        "INSERT INTO tool_hitl_settings (tool_name, mode) VALUES (?, ?)",
        ("browser_navigate", "sudo"),
    )
    now = int(time.time())
    db_conn.execute(
        "INSERT INTO sudo_grants (tool_name, caller_mind, granted_at, expires_at) VALUES (?, ?, ?, ?)",
        ("browser_navigate", "ada", now, now + 900),
    )
    db_conn.commit()

    result = await _check_hitl("browser_navigate", "ada")
    assert result == "ada"


async def test_hitl_gate_mode_sudo_without_grant_creates_request(
    db_conn: RawSQL,
) -> None:
    from hitl import HITLPendingException, _check_hitl

    db_conn.execute(
        "INSERT INTO tool_hitl_settings (tool_name, mode) VALUES (?, ?)",
        ("browser_navigate", "sudo"),
    )
    db_conn.commit()

    with pytest.raises(HITLPendingException):
        await _check_hitl("browser_navigate", "ada")


async def test_hitl_gate_mode_sudo_with_expired_grant_creates_request(
    db_conn: RawSQL,
) -> None:
    from hitl import HITLPendingException, _check_hitl

    db_conn.execute(
        "INSERT INTO tool_hitl_settings (tool_name, mode) VALUES (?, ?)",
        ("browser_navigate", "sudo"),
    )
    now = int(time.time())
    db_conn.execute(
        "INSERT INTO sudo_grants (tool_name, caller_mind, granted_at, expires_at) VALUES (?, ?, ?, ?)",
        ("browser_navigate", "ada", now - 1000, now - 100),
    )
    db_conn.commit()

    with pytest.raises(HITLPendingException):
        await _check_hitl("browser_navigate", "ada")


async def test_hitl_gate_no_setting_defaults_to_off(
    db_conn: RawSQL,
) -> None:
    from hitl import _check_hitl

    result = await _check_hitl("nonexistent_tool", "ada")
    assert result == "ada"


async def test_hitl_request_has_uuid_id(db_conn: RawSQL) -> None:
    from hitl import HITLPendingException, _check_hitl

    db_conn.execute(
        "INSERT INTO tool_hitl_settings (tool_name, mode) VALUES (?, ?)",
        ("test_tool", "on"),
    )
    db_conn.commit()

    with pytest.raises(HITLPendingException) as exc_info:
        await _check_hitl("test_tool", "ada")

    uuid.UUID(exc_info.value.request_id)


async def test_hitl_request_expires_in_5_minutes(db_conn: RawSQL) -> None:
    from hitl import HITLPendingException, _check_hitl

    db_conn.execute(
        "INSERT INTO tool_hitl_settings (tool_name, mode) VALUES (?, ?)",
        ("test_tool", "on"),
    )
    db_conn.commit()

    with pytest.raises(HITLPendingException) as exc_info:
        await _check_hitl("test_tool", "ada")

    row = db_conn.execute(
        "SELECT created_at, expires_at FROM hitl_requests WHERE id = ?",
        (exc_info.value.request_id,),
    ).fetchone()
    assert row is not None
    diff = row["expires_at"] - row["created_at"]
    assert 295 <= diff <= 305  # approximately 300 seconds
