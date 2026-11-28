"""B19: a key made at sign-in is accepted by the gateway, the memory store and
the tools as that person, and refused by all three once it is ended or its
time has passed.

The three real applications over the one scratch Postgres. The sign-in is
recorded with `core.record_sign_in` (what the sign-in story will call); the
clock is the record's own (`core.now`), moved by the test, so nothing sleeps.
Each service answers one route that a person's own key opens.
"""

from __future__ import annotations

import time
from collections.abc import Generator

import httpx
import pytest
from fastapi.testclient import TestClient

from hive_data import access, core
from tests.scratch import run

HOUR = 3600.0
NOW = 1_800_000_000.0


@pytest.fixture()
def clock(monkeypatch) -> dict:
    moment = {"t": NOW}
    monkeypatch.setattr(core, "now", lambda: moment["t"])
    return moment


@pytest.fixture()
def tools_client() -> Generator[TestClient, None, None]:
    from server import create_app

    with TestClient(create_app()) as entered:
        yield entered


@pytest.fixture()
def gateway_client() -> Generator[TestClient, None, None]:
    from comms import server

    with TestClient(server.app) as entered:
        yield entered


def _administrator(name: str) -> None:
    run(_make_admin(name))


async def _make_admin(name: str) -> None:
    await core.create_user(username=name, password_hash="x", created_at=int(NOW))
    await core.create_group(name=access.ADMINS_GROUP, created_at=int(NOW))
    user = next(u for u in await core.list_users() if u["username"] == name)
    await core.add_group_member(group_name=access.ADMINS_GROUP, user_id=user["id"])


def _memory_status(key: str) -> int:
    from lucent_api.server import create_app

    async def ask() -> int:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://lucent") as client:
            return (await client.get(
                "/graph/data", headers={"Authorization": f"Bearer {key}"})).status_code

    return run(ask())


def _statuses(gateway: TestClient, tools: TestClient, key: str) -> tuple[int, int, int]:
    bearer = {"Authorization": f"Bearer {key}"}
    return (gateway.get("/broker/minds", headers=bearer).status_code,
            _memory_status(key),
            tools.get("/hitl/waiting", headers=bearer).status_code)


def test_a_sign_in_key_is_that_person_at_all_three_until_it_is_ended_or_its_time_passes(
    gateway_client: TestClient, tools_client: TestClient, clock: dict,
) -> None:
    _administrator("daniel")
    # Another live key in the record, as on a real hive: a store with no live
    # key at all answers 503 rather than 401.
    run(core.upsert_credential(
        holder="ada", username="daniel", role="mind",
        token_sha=access.digest("ada-key-0123456789"), created_at=NOW))
    ended_early, runs_out = "sign-in-key-ended-0123456789", "sign-in-key-runs-out-0123456789"
    for key in (ended_early, runs_out):
        run(core.record_sign_in(username="daniel", token_sha=access.digest(key),
                                ends_at=NOW + 12 * HOUR, created_at=NOW))

    for key in (ended_early, runs_out):
        assert _statuses(gateway_client, tools_client, key) == (200, 200, 200)
    assert _statuses(gateway_client, tools_client, "nobody-recorded-this-key") == (401, 401, 401)

    run(core.end_sign_in(access.digest(ended_early), at=NOW + 60))
    clock["t"] = NOW + 60
    assert _statuses(gateway_client, tools_client, ended_early) == (401, 401, 401)
    assert _statuses(gateway_client, tools_client, runs_out) == (200, 200, 200)

    clock["t"] = NOW + 12 * HOUR  # the very moment it runs out
    assert _statuses(gateway_client, tools_client, runs_out) == (401, 401, 401)
