"""A person's own key: made by the console, accepted for the minds that person owns (requirement 19).

The gateway is the real application over ASGI on the suite's scratch Postgres,
so "accepted" and "refused" are its own answers to the question a mind asks
(the forward route), and the console's own functions are called, not
imitated. Protected behaviour 2, widened: a key reaches only what it should.
"""
import httpx
import pytest

import configuration_data
import credentials_data
import holder_credentials
import key_migration
import mind_key_files
import mind_runtime
from fastapi import HTTPException
from hive_site import testing

from .test_replace_mind_key import ADA_ID, ADMIN, BOB_ID, OLD, BOB, hive  # noqa: F401
from .test_key_migration import CAROL_ID, old_hive  # noqa: F401

SLOAN_MIND_ID = "6a1c0000-0000-4000-8000-0000000000d0"
SLOAN_MIND_KEY = "dora-own-key-0123456789"
ADA_URL, DORA_URL = "http://ada.test:8420", "http://dora.test:8420"


async def _asked(mind_key: str, presented: str) -> bool:
    """Whether the gateway lets `presented` change the mind that holds
    `mind_key`: its own answer on the forward route (a refusal is 401 or 403;
    anything else means it was let through to the delivery step, where the
    stand-in mind is not reachable)."""
    mind_id = ADA_ID if mind_key == OLD else SLOAN_MIND_ID
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"http://comms.test/broker/minds/{mind_id}/forward/health",
            headers={"Authorization": f"Bearer {presented}"})
    return response.status_code not in (401, 403)


@pytest.fixture
def two_owners(hive, monkeypatch):  # noqa: F811
    """Ada (daniel's, on this console) and Dora (sloan's), each with a key."""
    from hive_data import core

    async def seed():
        await core.upsert_mind(
            mind_id=SLOAN_MIND_ID, name="dora", gateway_url=DORA_URL, model="m", harness="dsh",
            registered_at=1.0, last_seen=1.0, owner="sloan")
        await holder_credentials._record_put("dora", "sloan", SLOAN_MIND_KEY, "mind")

    testing.run(seed())
    rows = [
        {"id": ADA_ID, "name": "ada", "gateway_url": ADA_URL, "owner": "daniel"},
        {"id": SLOAN_MIND_ID, "name": "dora", "gateway_url": DORA_URL, "owner": "sloan"},
    ]
    import route_access
    from unittest.mock import AsyncMock
    monkeypatch.setattr(route_access, "registered_minds", AsyncMock(return_value=rows))
    return hive


async def test_a_mind_on_another_machine_registered_by_its_pasted_key_counts_as_moved_and_says_so(old_hive):  # noqa: F811
    pasted = "carol-pasted-key-0123456789"
    await holder_credentials.replace_mind_key("carol", "daniel", CAROL_ID, pasted)

    report = await key_migration.account()

    carol = next(m for m in report["minds"] if m["mind"] == "carol")
    assert carol["state"] == "moved"
    assert "registered, key not stored here" in carol["note"]
    assert not any(line.startswith("carol") for line in report["blocking"])
    assert pasted not in str(report)


async def test_the_person_signed_in_to_a_request_is_what_the_console_presents_the_key_of():
    import auth

    seen = []

    async def endpoint(scope, receive, send):
        seen.append(mind_runtime._PERSON.get())

    async def resolved(conn):
        return {"username": "Daniel"}

    before = mind_runtime._PERSON.get()
    mind_runtime._PERSON.set(None)
    original = auth.resolve_user
    auth.resolve_user = resolved
    try:
        await mind_runtime.PersonScope(endpoint)({"type": "http", "headers": [], "path": "/x"}, None, None)
    finally:
        auth.resolve_user = original

    assert seen == ["daniel"]
    assert mind_runtime._PERSON.get() is None  # not left set after the request
    mind_runtime._PERSON.set(before)


def test_who_is_an_administrator_is_in_the_hive_database_whichever_sign_in_the_site_uses(console):
    from hive_site import store

    admin = console.admin()
    sloan = console.settled_user(admin, "sloan", "sloan-password-1")

    async def admins():
        return await store.group_usernames("admins")

    assert sloan.get("/api/waiting-work").status_code == 200
    assert "sloan" not in testing.run(admins())
    assert "daniel" in testing.run(admins())

    assert admin.put("/api/admin/groups/admins/members/sloan").status_code in (200, 204)
    assert sloan.get("/api/waiting-work").status_code == 200
    assert "sloan" in testing.run(admins())

    assert admin.delete("/api/admin/groups/admins/members/sloan").status_code in (200, 204)
    assert sloan.get("/api/waiting-work").status_code == 200
    assert "sloan" not in testing.run(admins())


