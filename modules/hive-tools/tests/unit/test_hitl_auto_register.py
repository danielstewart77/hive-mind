"""Tests for HITL auto-registration of gated tools.

Side effect: importing this module calls hitl_gate() which mutates the
module-level _REGISTERED_TOOLS set. Each test uses a unique tool name
prefixed with `__test_auto_register_` to avoid collisions with real tools
already registered via the normal import path (gmail, calendar, etc.).
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from tests.scratch import RawSQL


@pytest.fixture()
def only_these_tools() -> Iterator[object]:
    """Register tools against a registry of this test's own.

    `_REGISTERED_TOOLS` is module-global and every gated route in the repo
    adds to it at import time, so the count `sync_tool_registry` returns was
    being asserted as `>= 0` - true of every value the function can
    return, which is to say not asserted at all. Replacing the registry
    makes the count exact, which is what the one caller reads it for.
    """
    with patch("hitl._REGISTERED_TOOLS", set()) as registry:
        yield registry


def test_hitl_gate_records_tool_name_on_call() -> None:
    from hitl import _REGISTERED_TOOLS, hitl_gate

    name = "__test_auto_register_recorded"
    assert name not in _REGISTERED_TOOLS
    hitl_gate(name)
    assert name in _REGISTERED_TOOLS


async def test_sync_tool_registry_upserts_with_default_mode_on(
    db_conn: RawSQL, only_these_tools: object,
) -> None:
    from hitl import hitl_gate, sync_tool_registry

    name = "__test_auto_register_default_on"
    hitl_gate(name)

    assert await sync_tool_registry() == 1

    row = db_conn.execute(
        "SELECT mode, sudo_timeout_minutes FROM tool_hitl_settings WHERE tool_name = ?",
        (name,),
    ).fetchone()
    assert row is not None
    assert row["mode"] == "on"
    assert row["sudo_timeout_minutes"] == 15


async def test_sync_tool_registry_does_not_overwrite_user_edits(
    db_conn: RawSQL, only_these_tools: object,
) -> None:
    from hitl import hitl_gate, sync_tool_registry

    name = "__test_auto_register_user_edit_preserved"
    hitl_gate(name)
    await sync_tool_registry()

    # Simulate Daniel switching this tool to sudo mode in the control panel.
    db_conn.execute(
        "UPDATE tool_hitl_settings SET mode = 'sudo', sudo_timeout_minutes = 60 "
        "WHERE tool_name = ?",
        (name,),
    )
    db_conn.commit()

    # Nothing new on the second pass, because the tool already has a row -
    # and the edit above has to survive it, which is the whole reason the
    # seed is insert-if-absent.
    assert await sync_tool_registry() == 0

    row = db_conn.execute(
        "SELECT mode, sudo_timeout_minutes FROM tool_hitl_settings WHERE tool_name = ?",
        (name,),
    ).fetchone()
    assert row["mode"] == "sudo"
    assert row["sudo_timeout_minutes"] == 60


async def test_sync_preserves_manual_seed_off_modes(
    db_conn: RawSQL, only_these_tools: object,
) -> None:
    """Manual seed in db.py sets read endpoints to 'off'. Auto-register
    must not flip them to 'on'."""
    from hitl import seed_default_hitl_settings
    from hitl import hitl_gate, sync_tool_registry

    await seed_default_hitl_settings()

    # Simulate the tools/gmail.py import declaring the gate with the same
    # tool_name that the manual seed already set to 'off'.
    hitl_gate("gmail_read_emails")
    await sync_tool_registry()

    row = db_conn.execute(
        "SELECT mode FROM tool_hitl_settings WHERE tool_name = 'gmail_read_emails'"
    ).fetchone()
    assert row["mode"] == "off"


async def test_sync_returns_zero_when_all_tools_already_present(
    db_conn: RawSQL, only_these_tools: object,
) -> None:
    from hitl import hitl_gate, sync_tool_registry

    name = "__test_auto_register_idempotent"
    hitl_gate(name)

    assert await sync_tool_registry() == 1
    assert await sync_tool_registry() == 0

    row = db_conn.execute(
        "SELECT mode FROM tool_hitl_settings WHERE tool_name = ?", (name,)
    ).fetchone()
    assert row is not None
