"""An operator mind's key, and an address a mind may not move on its own.

Feature 834, requirement 20 (protected behaviour 2, widened): the console sends
a person's key to the address a mind registered, so an ordinary mind's own key
may not change an address; only the admin, an operator mind of the same owner
may. A mind that holds the operator role is read from the roles tables on every
request. Driven through the real gateway application over real rows.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient
from hive_data import roles

from comms import user_access
from comms_broker_pg import (  # noqa: F401 - fixtures
    broker_db, put_credential_row, put_mind, scratch_dsn,
)

ADMIN = "admin-bearer"  # secret-guard: allow
A, B, OP, FOREIGN = (
    "11111111-1111-4111-8111-11111111111%d" % i for i in (1, 2, 3, 4))
KEYS = {
    "alpha": "alpha-mind-key-for-tests",  # secret-guard: allow
    "beta": "beta-mind-key-for-tests",  # secret-guard: allow
    "ops": "ops-mind-key-for-tests",  # secret-guard: allow
    "other": "other-mind-key-for-tests",  # secret-guard: allow
    "orphan": "orphan-mind-key-for-tests",  # secret-guard: allow
    "ops-person": "ops-person-key-for-tests",  # secret-guard: allow
}
OLD = "http://alpha.old:8420"
NEW = "http://attacker.invalid:8420"


async def _seed() -> None:
    await put_mind(A, "alpha", owner="daniel", gateway_url=OLD)
    await put_mind(B, "beta", owner="daniel", gateway_url="http://beta.old:8420")
    await put_mind(OP, "ops", owner="daniel")
    await put_mind(FOREIGN, "other", owner="sloan", gateway_url="http://other.old:8420")
    await put_mind("55555555-5555-4555-8555-555555555555", "orphan", owner=None,
                   gateway_url="http://orphan.old:8420/")
    await put_credential_row("orphan", "", user_access.MIND,
                             user_access.digest(KEYS["orphan"]))
    # a person's key under the operator mind's own name: not a mind's key
    await put_credential_row("ops-person", "ops", user_access.USER_KEY,
                             user_access.digest(KEYS["ops-person"]))
    for holder, owner in (("alpha", "daniel"), ("beta", "daniel"),
                          ("ops", "daniel"), ("other", "sloan")):
        await put_credential_row(
            holder, owner, user_access.MIND, user_access.digest(KEYS[holder]))


@pytest.fixture
def world(monkeypatch, broker_db):
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", ADMIN)
    from comms import server

    importlib.reload(server)
    asyncio.run(_seed())
    with TestClient(server.app) as client:
        async def _role() -> int:
            found = await roles.role_by_slug("operator")
            if found:
                return found["id"]
            return (await roles.create_role("Operator", slug="operator"))["row"]["id"]

        role_id = client.portal.call(_role)

        def make_operator():
            client.portal.call(roles.set_mind_roles, OP, [role_id])

        def lose_role():
            client.portal.call(roles.set_mind_roles, OP, [])

        yield type("World", (), dict(
            client=client, make_operator=staticmethod(make_operator),
            lose_role=staticmethod(lose_role)))
        lose_role()


def _register(client, key, mind_id, name, url):
    return client.post(
        "/broker/minds", headers={"Authorization": f"Bearer {key}"},
        json={"mind_id": mind_id, "name": name, "gateway_url": url,
              "model": "sonnet", "harness": "claude_cli"})


def _address(client, name):
    rows = client.get("/broker/minds", headers={"Authorization": f"Bearer {ADMIN}"}).json()
    return next(r["gateway_url"] for r in rows if r["name"] == name)


def _asked(client, as_key, about):
    return client.post("/broker/owner-key-check", json={"key": about},
                       headers={"Authorization": f"Bearer {as_key}"}).json()


def test_an_ordinary_mind_cannot_move_its_own_address(world) -> None:
    refused = _register(world.client, KEYS["alpha"], A, "alpha", NEW)
    assert refused.status_code == 403
    assert _address(world.client, "alpha") == OLD


def test_boot_registration_of_the_same_address_still_works(world) -> None:
    assert _register(world.client, KEYS["alpha"], A, "alpha", OLD).status_code == 200
    assert _address(world.client, "alpha") == OLD
    # a trailing slash is the same address, not a move
    assert _register(world.client, KEYS["alpha"], A, "alpha", OLD + "/").status_code == 200


def test_an_ordinary_mind_cannot_move_another_minds_address(world) -> None:
    assert _register(world.client, KEYS["alpha"], B, "beta", NEW).status_code == 403
    assert _address(world.client, "beta") == "http://beta.old:8420"


def test_an_operator_mind_moves_a_mind_of_its_owner_and_not_another_owners(world) -> None:
    world.make_operator()
    assert _register(world.client, KEYS["ops"], A, "alpha", NEW).status_code == 200
    assert _address(world.client, "alpha") == NEW
    assert _register(world.client, KEYS["ops"], FOREIGN, "other", NEW).status_code == 403
    assert _address(world.client, "other") == "http://other.old:8420"


def test_an_operator_mind_cannot_rename_the_mind_it_moves(world) -> None:
    world.make_operator()
    assert _register(world.client, KEYS["ops"], A, "renamed", NEW).status_code == 403


def test_an_operators_key_is_accepted_for_a_same_owner_mind_only(world) -> None:
    world.make_operator()
    assert _asked(world.client, KEYS["alpha"], KEYS["ops"]) == {"owned": True}
    assert _asked(world.client, KEYS["other"], KEYS["ops"]) == {"owned": False}


def test_an_ordinary_minds_key_is_not_accepted_for_another_mind(world) -> None:
    assert _asked(world.client, KEYS["alpha"], KEYS["beta"]) == {"owned": False}
    assert _asked(world.client, KEYS["alpha"], KEYS["ops"]) == {"owned": False}


def test_a_mind_that_loses_the_role_is_refused_on_the_next_request(world) -> None:
    world.make_operator()
    assert _asked(world.client, KEYS["alpha"], KEYS["ops"]) == {"owned": True}
    assert _register(world.client, KEYS["ops"], A, "alpha", NEW).status_code == 200
    world.lose_role()
    assert _asked(world.client, KEYS["alpha"], KEYS["ops"]) == {"owned": False}
    assert _register(world.client, KEYS["ops"], A, "alpha", OLD).status_code == 403
    assert _address(world.client, "alpha") == NEW


def test_a_cut_off_operator_key_is_refused(world) -> None:
    world.make_operator()
    assert world.client.delete(
        "/credentials/ops", headers={"Authorization": f"Bearer {ADMIN}"}).status_code == 200
    assert _asked(world.client, KEYS["alpha"], KEYS["ops"]) == {"owned": False}


def test_an_operator_does_not_open_an_ownerless_mind_and_a_non_mind_key_is_no_operator(world) -> None:
    world.make_operator()
    assert _asked(world.client, KEYS["orphan"], KEYS["ops"]) == {"owned": False}
    assert _register(world.client, KEYS["ops"], "55555555-5555-4555-8555-555555555555",
                     "orphan", NEW).status_code == 403
    # a user-kind credential is never read as the operator mind's key
    assert _asked(world.client, KEYS["alpha"], KEYS["ops-person"]) == {"owned": False}


def test_an_operator_registers_a_same_owner_mind_at_its_address_but_cannot_rename_it(world) -> None:
    world.make_operator()
    assert _register(world.client, KEYS["ops"], B, "beta", "http://beta.old:8420").status_code == 200
    assert _register(world.client, KEYS["ops"], B, "gamma", "http://beta.old:8420").status_code == 403


def test_a_stored_address_with_a_trailing_slash_is_refreshed_without_it(world) -> None:
    assert _register(world.client, KEYS["alpha"], A, "alpha", OLD + "/").status_code == 200
    assert _register(world.client, KEYS["alpha"], A, "alpha", OLD).status_code == 200


def test_a_mind_registering_for_the_first_time_is_accepted(world) -> None:
    fresh = "66666666-6666-4666-8666-666666666666"
    client = world.client

    async def _credential():
        await put_credential_row("fresh", "daniel", user_access.MIND,
                                 user_access.digest("fresh-mind-key-for-tests"))  # secret-guard: allow

    client.portal.call(_credential)
    assert _register(client, "fresh-mind-key-for-tests",  # secret-guard: allow
                     fresh, "fresh", NEW).status_code == 200


def test_the_same_address_spelled_differently_is_not_a_move(world) -> None:
    for url in ("HTTP://Alpha.Old:8420", "http://alpha.old:8420/"):
        assert _register(world.client, KEYS["alpha"], A, "alpha", url).status_code == 200
    assert _register(world.client, KEYS["alpha"], A, "alpha",
                     "http://alpha.old:8421").status_code == 403


def test_a_changed_query_userinfo_or_ipv6_host_is_a_move(world) -> None:
    for url in (OLD + "?x=1", "http://u@alpha.old:8420", "http://[::1]:8420",
                "http://evil.invalid\\@alpha.old:8420"):
        assert _register(world.client, KEYS["alpha"], A, "alpha", url).status_code == 403, url
    assert _address(world.client, "alpha") == OLD


def test_a_mind_cannot_take_a_second_row_under_its_own_name(world) -> None:
    fresh = "66666666-6666-4666-8666-666666666666"
    for spelling in ("alpha", "Alpha", "ALPHA"):
        refused = _register(world.client, KEYS["alpha"], fresh, spelling, NEW)
        assert refused.status_code == 403, spelling
    rows = world.client.get(
        "/broker/minds", headers={"Authorization": f"Bearer {ADMIN}"}).json()
    assert [r["name"] for r in rows].count("alpha") == 1
    assert len(rows) == 5
    assert _address(world.client, "alpha") == OLD


def test_an_unreadable_roles_table_makes_an_operator_an_ordinary_mind(world, monkeypatch) -> None:
    """The role is read at every request; when the read fails the gateway
    refuses the operator's reach (403, not 500) rather than guess yes. The
    store is the boundary that fails here; `is_operator_mind` itself runs."""
    world.make_operator()
    assert _register(world.client, KEYS["ops"], A, "alpha", NEW).status_code == 200

    async def unreadable(*args, **kwargs):
        raise RuntimeError("the roles tables cannot be read")

    monkeypatch.setattr(roles, "roles_of", unreadable)
    refused = _register(world.client, KEYS["ops"], A, "alpha", OLD)
    assert refused.status_code == 403
    assert _address(world.client, "alpha") == NEW
    assert _asked(world.client, KEYS["alpha"], KEYS["ops"]) == {"owned": False}
