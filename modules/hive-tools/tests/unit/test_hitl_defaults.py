"""Tests for default HITL settings seeding."""

from __future__ import annotations
from tests.scratch import RawSQL



async def test_seed_creates_default_settings(db_conn: RawSQL) -> None:
    from hitl import seed_default_hitl_settings

    await seed_default_hitl_settings()

    rows = db_conn.execute("SELECT tool_name, mode FROM tool_hitl_settings").fetchall()
    tool_names = {row["tool_name"] for row in rows}

    # Should have entries for all expected tools
    assert "calendar_list_events" in tool_names
    assert "gmail_send_email" in tool_names
    assert "linkedin_post" in tool_names
    assert "docker_compose_up" in tool_names


async def test_seed_idempotent_does_not_overwrite(db_conn: RawSQL) -> None:
    from hitl import seed_default_hitl_settings

    await seed_default_hitl_settings()

    # Manually change a setting
    db_conn.execute(
        "UPDATE tool_hitl_settings SET mode = 'sudo' WHERE tool_name = 'gmail_send_email'"
    )
    db_conn.commit()

    # Seed again
    await seed_default_hitl_settings()

    row = db_conn.execute(
        "SELECT mode FROM tool_hitl_settings WHERE tool_name = 'gmail_send_email'"
    ).fetchone()
    # Should NOT be overwritten
    assert row["mode"] == "sudo"


async def test_default_modes_match_spec(db_conn: RawSQL) -> None:
    from hitl import seed_default_hitl_settings

    await seed_default_hitl_settings()

    def get_mode(tool_name: str) -> str:
        row = db_conn.execute(
            "SELECT mode FROM tool_hitl_settings WHERE tool_name = ?",
            (tool_name,),
        ).fetchone()
        return row["mode"] if row else "missing"

    # Read tools should be off
    assert get_mode("calendar_list_events") == "off"
    assert get_mode("calendar_get_event") == "off"
    assert get_mode("gmail_read_emails") == "off"
    assert get_mode("gmail_get_email") == "off"

    # Write tools should be on
    assert get_mode("gmail_send_email") == "on"
    assert get_mode("calendar_create_event") == "on"
    assert get_mode("linkedin_post") == "on"
    assert get_mode("docker_compose_up") == "on"

    # Training tools are driven from the console, whose login is the gate;
    # gating them would page the person who just clicked the button.
    assert get_mode("training_curate") == "off"
    assert get_mode("training_export") == "off"
    assert get_mode("training_train") == "off"
