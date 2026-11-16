"""The model a DSH conversation runs on, as its mind's roles allow (10.18.5).

Protected behaviours 1 and 2 of Role Management for the one grant the
gateway decides: a DSH mind under role management runs only on a model one
of its roles names, and a role changed between two turns is obeyed on the
second. The roles are real rows in the scratch database, read through the
same `hive_data.roles` the console writes them with.
"""
# ruff: noqa: F811 - pytest fixtures are imported, then named as parameters
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from hive_data import roles

from comms_pg_fixture import (  # noqa: F401 - fixtures
    close_manager,
    comms_db,
    make_manager,
    register_mind,
    scratch_dsn,
    seed_session,
)


def _run(coro):
    return asyncio.run(coro)


async def _role(model: str | None) -> int:
    made = await roles.create_role(f"Role {uuid.uuid4().hex[:8]}", model=model)
    return made["row"]["id"]


async def _mind(harness: str = "dsh_cli", default: str = "claude-haiku-5-5") -> str:
    mind_id = f"mind-{uuid.uuid4().hex[:8]}"
    await register_mind(mind_id, harness=harness, model=default)
    return mind_id


def test_a_managed_dsh_conversation_runs_on_a_model_its_roles_name(comms_db):
    async def scenario():
        mgr = await make_manager()
        mind = await _mind()
        await roles.set_mind_roles(mind, [await _role("claude-opus-5-5")])
        await seed_session("s1", mind_id=mind, harness="dsh", model="gpt-6.1-terra")
        session = await mgr._get_row("s1")

        chosen = await mgr.role_model(session)

        assert chosen == "claude-opus-5-5"
        await close_manager(mgr)

    _run(scenario())


def test_a_role_changed_between_turns_moves_the_next_turn_onto_its_model(comms_db):
    async def scenario():
        mgr = await make_manager()
        mind = await _mind()
        role = await _role("claude-opus-5-5")
        await roles.set_mind_roles(mind, [role])
        await seed_session("s2", mind_id=mind, harness="dsh", model="claude-opus-5-5")
        assert await mgr.role_model(await mgr._get_row("s2")) == "claude-opus-5-5"

        await roles.update_role(role, model="claude-sonnet-5-5")

        assert await mgr.role_model(await mgr._get_row("s2")) == "claude-sonnet-5-5"
        await close_manager(mgr)

    _run(scenario())


def test_a_managed_dsh_mind_whose_roles_name_no_model_is_refused_a_turn(comms_db):
    async def scenario():
        mgr = await make_manager()
        mind = await _mind()
        await roles.set_mind_roles(mind, [await _role(None)])
        await seed_session("s3", mind_id=mind, harness="dsh", model="claude-haiku-5-5")

        with pytest.raises(ValueError, match="names a model"):
            await mgr.role_model(await mgr._get_row("s3"))
        await close_manager(mgr)

    _run(scenario())


def test_switching_a_managed_dsh_conversation_to_a_model_no_role_names_is_refused(comms_db):
    async def scenario():
        mgr = await make_manager()
        mind = await _mind()
        await roles.set_mind_roles(mind, [await _role("claude-opus-5-5")])
        await seed_session("s4", mind_id=mind, harness="dsh", model="claude-opus-5-5")
        with patch.object(mgr, "mind_models", new=AsyncMock(return_value=[
                {"name": "claude-opus-5-5"}, {"name": "gpt-6.1-terra"}])), \
                patch.object(mgr, "_kill_process", new=AsyncMock()) as killed, \
                patch.object(mgr, "_spawn", new=AsyncMock()) as spawned:
            with pytest.raises(ValueError, match="roles allow"):
                await mgr.switch_model("s4", "gpt-6.1-terra")
        assert killed.await_count == 0 and spawned.await_count == 0
        assert (await mgr._get_row("s4"))["model"] == "claude-opus-5-5"
        await close_manager(mgr)

    _run(scenario())


def test_an_unmanaged_or_non_dsh_conversation_keeps_the_model_it_names(comms_db):
    async def scenario():
        mgr = await make_manager()
        unmanaged = await _mind()
        claude = await _mind(harness="claude_cli")
        await roles.set_mind_roles(claude, [await _role("claude-opus-5-5")])
        await seed_session("s5", mind_id=unmanaged, harness="dsh", model="gpt-6.1-terra")
        await seed_session("s6", mind_id=claude, harness="claude", model="claude-haiku-5-5",
                           surface_id="124")

        assert await mgr.role_model(await mgr._get_row("s5")) == "gpt-6.1-terra"
        assert await mgr.role_model(await mgr._get_row("s6")) == "claude-haiku-5-5"
        await close_manager(mgr)

    _run(scenario())
