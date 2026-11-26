"""A role never decides which model a conversation runs on (feature 893).

A mind runs on the model it is set to and a conversation asked for a model
gets that one, whatever tier the mind's roles suggest. The roles are real rows in the
scratch database, written through the same `hive_data.roles` the console
writes them with, and the mind is one under role management, which is the
only kind the old rule ever moved.
"""
# ruff: noqa: F811 - pytest fixtures are imported, then named as parameters
from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from hive_data import roles

from comms.sessions import SessionManager
from comms_pg_fixture import (  # noqa: F401 - fixtures
    close_manager,
    comms_db,
    fetchval,
    make_manager,
    register_mind,
    scratch_dsn,
    seed_session,
)


def _run(coro):
    return asyncio.run(coro)


async def _role(suggested_tier: str | None = "high") -> int:
    made = await roles.create_role(f"Role {uuid.uuid4().hex[:8]}", tools=["files"],
                                   suggested_tier=suggested_tier)
    return made["row"]["id"]


async def _mind(harness: str = "dsh_cli", default: str = "claude-haiku-5-5") -> str:
    mind_id = f"mind-{uuid.uuid4().hex[:8]}"
    await register_mind(mind_id, harness=harness, model=default)
    return mind_id


@contextmanager
def _spawns_captured(spawns: list):
    """The mind's container is not ours: what the spawn was told is kept."""
    async def blocks(**_kw):
        return "<soul>seed</soul>"

    async def spawn(_self, session_id, spawn_model, **_kw):
        spawns.append(spawn_model)

    with patch("comms.bootstrap_loader.compose_prompt_blocks", blocks), \
            patch.object(SessionManager, "_spawn", spawn):
        yield


async def _model_of(session_id: str) -> str:
    return await fetchval("SELECT model FROM core.sessions WHERE id = $1", session_id) or ""


def test_a_mind_set_to_a_model_no_role_of_its_names_opens_its_conversations_on_it(comms_db):
    async def scenario():
        mgr = await make_manager()
        mind = await _mind(default="gemma4:26b")
        await roles.set_mind_roles(mind, [await _role()])
        spawns: list = []
        with _spawns_captured(spawns):
            created = await mgr.create_session(
                surface="telegram", surface_id="131", client_ref="131", mind_id=mind)

        assert await _model_of(created["id"]) == "gemma4:26b"
        assert spawns == ["gemma4:26b"]
        await close_manager(mgr)

    _run(scenario())


def test_a_conversation_asked_for_a_model_no_role_names_opens_on_that_model(comms_db):
    async def scenario():
        mgr = await make_manager()
        mind = await _mind(default="claude-haiku-5-5")
        await roles.set_mind_roles(mind, [await _role()])
        spawns: list = []
        with _spawns_captured(spawns):
            created = await mgr.create_session(
                surface="telegram", surface_id="132", client_ref="132", mind_id=mind,
                model="gpt-6.1-terra")

        assert await _model_of(created["id"]) == "gpt-6.1-terra"
        assert spawns == ["gpt-6.1-terra"]
        await close_manager(mgr)

    _run(scenario())


def test_a_turn_runs_on_its_conversations_model_when_no_role_names_it(comms_db):
    async def scenario():
        mgr = await make_manager()
        mind = await _mind()
        await roles.set_mind_roles(mind, [await _role()])
        await seed_session("s7", mind_id=mind, harness="dsh", model="gpt-6.1-terra",
                           surface_id="125")
        with patch.object(mgr, "mind_model_row", new=AsyncMock(return_value=None)), \
                patch.object(mgr, "_kill_process", new=AsyncMock()), \
                patch.object(mgr, "_spawn", new=AsyncMock()) as spawned, \
                patch.object(mgr, "_routing_for", new=AsyncMock(return_value={})):
            # The mind itself is not reachable from a test; the turn ends there,
            # after the spawn this asserts on.
            with pytest.raises(ValueError, match="No mind container URL"):
                async for _event in mgr.send_message("s7", "hello"):
                    pass
        assert spawned.await_args.args[1] == "gpt-6.1-terra"
        assert await _model_of("s7") == "gpt-6.1-terra"
        await close_manager(mgr)

    _run(scenario())


def test_a_conversation_is_switched_to_a_model_no_role_names(comms_db):
    async def scenario():
        mgr = await make_manager()
        mind = await _mind()
        await roles.set_mind_roles(mind, [await _role()])
        await seed_session("s4", mind_id=mind, harness="dsh", model="claude-opus-5-5")
        with patch.object(mgr, "mind_models", new=AsyncMock(return_value=[
                {"name": "claude-opus-5-5"}, {"name": "gpt-6.1-terra"}])), \
                patch.object(mgr, "_kill_process", new=AsyncMock()), \
                patch.object(mgr, "_spawn", new=AsyncMock()):
            await mgr.switch_model("s4", "gpt-6.1-terra")
        assert await _model_of("s4") == "gpt-6.1-terra"
        await close_manager(mgr)

    _run(scenario())


def test_a_minds_own_credential_reads_its_own_grants_and_no_other_minds(comms_db, monkeypatch):
    import importlib

    from fastapi.testclient import TestClient

    from comms import user_access

    async def setup():
        import hive_data
        await hive_data.connect()
        mine, other = await _mind(), await _mind()
        await roles.set_mind_roles(mine, [await _role()])
        await user_access.put_credential(mine, "daniel", "mind-secret", role="mind")
        await hive_data.close(drain_timeout=5.0)
        return mine, other

    mine, other = _run(setup())
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-bearer")
    from comms import server as server_module
    importlib.reload(server_module)
    with TestClient(server_module.app, headers={"Authorization": "Bearer mind-secret"}) as client:
        own = client.get(f"/minds/{mine}/grants")
        foreign = client.get(f"/minds/{other}/grants")

    assert own.status_code == 200
    assert own.json()["managed"] is True and own.json()["tools"] == ["files"]
    assert foreign.status_code == 404
