"""B14: a mind's key recorded once is accepted by the memory store and by the
tools, and refused by both once it is cut off once.

The real route handlers of both services, over the one scratch Postgres. The
key is recorded and cut off with the writes the hive site makes
(`core.upsert_credential`, `broker_ops.revoke_live_credential`); nothing is
registered with either service.
"""

from __future__ import annotations

import time
from collections.abc import Generator

import httpx
import pytest
from fastapi.testclient import TestClient

from hive_data import access, broker_ops, core
from tests.scratch import run


@pytest.fixture()
def tools_client() -> Generator[TestClient, None, None]:
    from server import create_app

    with TestClient(create_app()) as entered:
        yield entered


def _memory_store_status(key: str) -> int:
    from lucent_api.server import create_app

    async def ask() -> int:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://lucent") as client:
            return (await client.get("/memory/list", headers={"Authorization": f"Bearer {key}"})).status_code

    return run(ask())


def _tools_status(client: TestClient, key: str) -> int:
    return client.get("/hitl/no-such-request",
                      headers={"Authorization": f"Bearer {key}"}).status_code


def test_a_minds_key_recorded_once_is_accepted_by_the_memory_store_and_the_tools_until_it_is_cut_off_once(
    tools_client: TestClient,
) -> None:
    key = "bilby-key-recorded-once-0123456789"
    run(core.upsert_credential(
        holder="bilby", username="daniel", role="mind",
        token_sha=access.digest(key), created_at=time.time(), revoked_at=None))
    # Another mind's live key stays in the record, as on a real hive: a store
    # with no live key at all answers 503 rather than 401.
    run(core.upsert_credential(
        holder="ada", username="daniel", role="mind",
        token_sha=access.digest("ada-key-0123456789"), created_at=time.time(),
        revoked_at=None))
    assert _memory_store_status("a-key-nobody-recorded") == 401
    assert _tools_status(tools_client, "a-key-nobody-recorded") == 401

    assert _memory_store_status(key) == 200
    assert _tools_status(tools_client, key) == 404  # authenticated; no such request

    run(broker_ops.revoke_live_credential("bilby", time.time()))

    assert _memory_store_status(key) == 401
    assert _tools_status(tools_client, key) == 401
