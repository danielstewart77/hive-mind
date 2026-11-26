"""A person's key opens a mind's settings only for the mind that person owns.

Feature 834, requirement 19 (protected behaviour 2, widened): the gateway holds
a user's key like a mind's, and a mind may ask it one question about a key it
was handed - "is this my owner's?" - and is answered yes or no and nothing else.
Driven through the real gateway application over its real credential table.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient

from comms import user_access
from comms_broker_pg import (  # noqa: F401 - fixtures
    broker_db, put_credential_row, put_mind, scratch_dsn,
)

ALPHA, BETA = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
ADMIN = "admin-bearer"  # secret-guard: allow
ALPHA_KEY = "alpha-mind-key-for-tests"  # secret-guard: allow
BETA_KEY = "beta-mind-key-for-tests"  # secret-guard: allow
DANIEL_KEY = "daniel-person-key-for-tests"  # secret-guard: allow
SLOAN_KEY = "sloan-person-key-for-tests"  # secret-guard: allow


def _run(coro):
    return asyncio.run(coro)


async def _seed() -> None:
    await put_mind(ALPHA, "alpha", owner="daniel")
    await put_mind(BETA, "beta", owner="sloan")
    for holder, user, key in (("alpha", "daniel", ALPHA_KEY), ("beta", "sloan", BETA_KEY)):
        await put_credential_row(holder, user, user_access.MIND, user_access.digest(key))
    for user, key in (("daniel", DANIEL_KEY), ("sloan", SLOAN_KEY)):
        await put_credential_row(user, user, user_access.USER_KEY, user_access.digest(key))


@pytest.fixture
def client(monkeypatch, broker_db):
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", ADMIN)
    from comms import server

    importlib.reload(server)
    _run(_seed())
    with TestClient(server.app) as test_client:
        yield test_client


def _asked(client, as_key: str, about: str):
    return client.post("/broker/owner-key-check", json={"key": about},
                       headers={"Authorization": f"Bearer {as_key}"})


def test_a_mind_is_told_yes_for_its_owners_key_and_no_for_another_users(client) -> None:
    assert _asked(client, ALPHA_KEY, DANIEL_KEY).json() == {"owned": True}
    assert _asked(client, ALPHA_KEY, SLOAN_KEY).json() == {"owned": False}
    assert _asked(client, BETA_KEY, SLOAN_KEY).json() == {"owned": True}
    assert _asked(client, BETA_KEY, DANIEL_KEY).json() == {"owned": False}


def test_a_value_that_is_not_a_users_key_is_never_a_yes(client) -> None:
    # Another mind's key, the mind's own key, the admin credential, nonsense.
    for value in (BETA_KEY, ALPHA_KEY, ADMIN, "never-issued-for-tests", ""):
        assert _asked(client, ALPHA_KEY, value).json() == {"owned": False}


def test_a_replaced_or_cut_off_users_key_is_refused_on_the_next_question(client) -> None:
    assert _asked(client, ALPHA_KEY, DANIEL_KEY).json() == {"owned": True}
    replaced = client.put(
        "/credentials/daniel", headers={"Authorization": f"Bearer {ADMIN}"},
        json={"username": "daniel", "token": "daniel-new-key-for-tests",  # secret-guard: allow
              "role": "user"})
    assert replaced.status_code == 200
    assert _asked(client, ALPHA_KEY, DANIEL_KEY).json() == {"owned": False}
    assert _asked(client, ALPHA_KEY, "daniel-new-key-for-tests").json() == {"owned": True}  # secret-guard: allow
    assert client.delete("/credentials/daniel", headers={"Authorization": f"Bearer {ADMIN}"}).status_code == 200
    assert _asked(client, ALPHA_KEY, "daniel-new-key-for-tests").json() == {"owned": False}  # secret-guard: allow


def test_only_a_minds_own_key_may_ask_the_question(client) -> None:
    # No oracle: a person's key, the admin credential and no credential at all
    # cannot test a value against anybody's minds.
    _run(put_credential_row("scheduler", "daniel", user_access.SERVICE,
                            user_access.digest("scheduler-key-for-tests")))  # secret-guard: allow
    for caller in (DANIEL_KEY, ADMIN, "never-issued-for-tests", "", "scheduler-key-for-tests"):  # secret-guard: allow
        assert _asked(client, caller, DANIEL_KEY).status_code == 401


def test_a_users_key_opens_no_route_of_the_gateway(client) -> None:
    headers = {"Authorization": f"Bearer {DANIEL_KEY}"}
    for path in ("/broker/minds", "/sessions", "/credentials"):
        assert client.get(path, headers=headers).status_code == 401, path


def test_the_gateway_refuses_a_users_key_under_a_mind_s_name_or_another_user_s(client) -> None:
    admin = {"Authorization": f"Bearer {ADMIN}"}
    other_user = client.put("/credentials/daniel", headers=admin, json={
        "username": "sloan", "token": "x-key-for-tests", "role": "user"})  # secret-guard: allow
    a_mind = client.put("/credentials/alpha", headers=admin, json={
        "username": "alpha", "token": "y-key-for-tests", "role": "user"})  # secret-guard: allow
    assert other_user.status_code == 400
    assert a_mind.status_code == 400
    assert _asked(client, ALPHA_KEY, DANIEL_KEY).json() == {"owned": True}


def test_a_mind_with_no_owner_and_a_mind_that_changed_owner_follow_the_registry(client) -> None:
    _run(put_mind("33333333-3333-4333-8333-333333333333", "gamma"))
    _run(put_credential_row("gamma", "daniel", user_access.MIND, user_access.digest("gamma-key-for-tests")))  # secret-guard: allow
    assert _asked(client, "gamma-key-for-tests", DANIEL_KEY).json() == {"owned": False}  # secret-guard: allow
    moved = client.put(f"/broker/minds/{ALPHA}/owner", headers={"Authorization": f"Bearer {ADMIN}"},
                       json={"owner": "sloan"})
    assert moved.status_code == 200
    assert _asked(client, ALPHA_KEY, DANIEL_KEY).json() == {"owned": False}
    assert _asked(client, ALPHA_KEY, SLOAN_KEY).json() == {"owned": True}
