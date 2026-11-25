"""Which minds hold a role, as the gateway tells a mind that asks.

Protected behaviour 5 of the workflow documents and role skills feature: a
mind that asks which minds hold a role is told the minds of its own owner that
hold it at that moment. The roles and minds are real rows in the scratch
database, the route is the real app, and the asker presents a mind's own
credential the way a mind does.
"""
# ruff: noqa: F811 - pytest fixtures are imported, then named as parameters
from __future__ import annotations

import asyncio
import importlib
import uuid

import pytest
from fastapi.testclient import TestClient
from hive_data import roles

from comms_pg_fixture import comms_db, register_mind, scratch_dsn  # noqa: F401 - fixtures

ADMIN_TOKEN = "admin-bearer"  # secret-guard: allow


def _run(coro):
    return asyncio.run(coro)


async def _mind(owner: str, name: str) -> str:
    # Names are unique across the scratch database's life, so each is made new.
    mind_id = f"mind-{uuid.uuid4().hex[:8]}"
    await register_mind(mind_id, name=f"{name}-{mind_id}", owner=owner)
    return mind_id


async def _role() -> tuple[int, str]:
    slug = f"role-{uuid.uuid4().hex[:8]}"
    made = await roles.create_role(slug, slug=slug)
    return made["row"]["id"], slug


@pytest.fixture
def world(comms_db, monkeypatch):
    """An asker mind of daniel's, a second mind of daniel's, a mind of
    another owner, one role, and a client that presents the asker's own
    credential. Role changes go through the app's own loop and connection."""
    from comms import user_access

    # The scratch database outlives a test, and a value belongs to one holder.
    asker_token = f"asker-{uuid.uuid4().hex}"  # secret-guard: allow

    async def setup():
        import hive_data
        await hive_data.connect()
        asker = await _mind("daniel", "asker")
        sibling = await _mind("Daniel ", "sibling")
        stranger = await _mind("sloan", "stranger")
        role_id, slug = await _role()
        await user_access.put_credential(asker, "daniel", asker_token, role="mind")
        await hive_data.close(drain_timeout=5.0)
        return asker, sibling, stranger, role_id, slug

    asker, sibling, stranger, role_id, slug = _run(setup())
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", ADMIN_TOKEN)
    from comms import server as server_module
    importlib.reload(server_module)
    with TestClient(server_module.app,
                    headers={"Authorization": f"Bearer {asker_token}"}) as client:
        def hold(mind_id, *more):
            client.portal.call(roles.set_mind_roles, mind_id, [role_id, *more])

        def release(mind_id):
            client.portal.call(roles.set_mind_roles, mind_id, [])

        def ask(as_token=None):
            headers = {"Authorization": f"Bearer {as_token}"} if as_token else None
            return client.get(f"/roles/{slug}/holders", headers=headers)

        yield type("World", (), dict(
            asker=asker, sibling=sibling, stranger=stranger, slug=slug,
            hold=staticmethod(hold), release=staticmethod(release),
            ask=staticmethod(ask), client=client))


def test_a_role_given_to_a_mind_of_the_askers_owner_is_named_in_the_next_ask(world):
    assert world.ask().json()["minds"] == []

    world.hold(world.sibling)

    answer = world.ask()
    assert answer.status_code == 200
    assert answer.json() == {
        "role": world.slug, "exists": True,
        "minds": [{"mind_id": world.sibling, "name": f"sibling-{world.sibling}"}],
    }


def test_a_role_taken_away_is_gone_from_the_next_ask(world):
    world.hold(world.sibling)
    assert [m["mind_id"] for m in world.ask().json()["minds"]] == [world.sibling]

    world.release(world.sibling)

    assert world.ask().json()["minds"] == []


def test_a_mind_of_another_owner_holding_the_role_is_not_named_to_the_asker(world):
    world.hold(world.stranger)
    world.hold(world.sibling)

    ids = [m["mind_id"] for m in world.ask().json()["minds"]]

    assert ids == [world.sibling]


def test_the_admin_credential_is_told_the_holders_of_every_owner(world):
    world.hold(world.stranger)
    world.hold(world.sibling)

    answer = world.ask(as_token=ADMIN_TOKEN)

    assert sorted(m["mind_id"] for m in answer.json()["minds"]) == sorted(
        [world.stranger, world.sibling])


def test_a_role_nobody_holds_and_a_role_that_does_not_exist_answer_an_empty_list(world):
    unheld = world.ask()
    missing = world.client.get("/roles/no-such-role-anywhere/holders")

    assert unheld.status_code == 200
    assert unheld.json() == {"role": world.slug, "exists": True, "minds": []}
    assert missing.status_code == 200
    assert missing.json() == {"role": "no-such-role-anywhere", "exists": False, "minds": []}


def test_the_answer_names_each_holder_by_id_and_name_and_nothing_else(world):
    world.hold(world.sibling)

    entry = world.ask().json()["minds"][0]

    assert set(entry) == {"mind_id", "name"}


def test_a_removed_mind_credential_frees_its_key_for_another_mind_but_a_revoked_one_does_not(
        comms_db):
    """A failed creation removes the row it made, so the pasted key can be
    pasted again; a cut-off keeps its row and the key stays refused."""
    from comms import user_access

    key = f"pasted-{uuid.uuid4().hex}"  # secret-guard: allow
    first, second, third = (f"h-{uuid.uuid4().hex[:8]}" for _ in range(3))

    async def go():
        import hive_data
        await hive_data.connect()
        try:
            await user_access.put_credential(first, "daniel", key, role="mind")
            await user_access.revoke_credential(first)
            with pytest.raises(ValueError, match="already held"):
                await user_access.put_credential(second, "daniel", key, role="mind")
            assert await user_access.remove_mind_credential(first) is True
            await user_access.put_credential(third, "daniel", key, role="mind")
            return (await user_access.credential_for_token(key)).holder
        finally:
            await hive_data.close(drain_timeout=5.0)

    assert asyncio.run(go()) == third
