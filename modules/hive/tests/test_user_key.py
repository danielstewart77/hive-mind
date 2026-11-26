"""A person's own key: made by the console, accepted for the minds that person owns (requirement 19).

The gateway is the real application over ASGI on the suite's scratch Postgres,
so "accepted" and "refused" are its own answers to the question a mind asks
(`/broker/owner-key-check`), and the console's own functions are called, not
imitated. Protected behaviour 2, widened: a key reaches only what it should.
"""
import httpx
import pytest

import configuration_data
import credentials_data
import holder_credentials
import key_migration
import mind_runtime
from fastapi import HTTPException
from hive_site import testing

from .test_replace_mind_key import ADA_ID, ADMIN, BOB_ID, OLD, BOB, hive  # noqa: F401
from .test_key_migration import CAROL_ID, old_hive, MigrationTools  # noqa: F401

SLOAN_MIND_ID = "6a1c0000-0000-4000-8000-0000000000d0"
SLOAN_MIND_KEY = "dora-own-key-0123456789"
ADA_URL, DORA_URL = "http://ada.test:8420", "http://dora.test:8420"


async def _asked(mind_key: str, presented: str) -> bool:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://comms.test/broker/owner-key-check", json={"key": presented},
            headers={"Authorization": f"Bearer {mind_key}"})
    assert response.status_code == 200
    return response.json()["owned"]


@pytest.fixture
def two_owners(hive, monkeypatch):  # noqa: F811
    """Ada (daniel's, on this console) and Dora (sloan's), each with a key."""
    from comms import user_access
    from hive_data import core

    async def seed():
        await core.upsert_mind(
            mind_id=SLOAN_MIND_ID, name="dora", gateway_url=DORA_URL, model="m", harness="dsh",
            registered_at=1.0, last_seen=1.0, owner="sloan")
        await user_access.put_credential("dora", "sloan", SLOAN_MIND_KEY, role="mind")

    testing.run(seed())
    rows = [
        {"id": ADA_ID, "name": "ada", "gateway_url": ADA_URL, "owner": "daniel"},
        {"id": SLOAN_MIND_ID, "name": "dora", "gateway_url": DORA_URL, "owner": "sloan"},
    ]
    import route_access
    from unittest.mock import AsyncMock
    monkeypatch.setattr(route_access, "registered_minds", AsyncMock(return_value=rows))
    return hive


async def test_a_users_key_is_made_on_first_need_kept_and_accepted_only_for_minds_that_user_owns(two_owners):
    key = await holder_credentials.user_key("daniel")

    assert key and len(key) >= 32
    assert credentials_data._read_env_value(two_owners.env, "USER_KEY_DANIEL") == key
    assert await holder_credentials.user_key("daniel") == key  # kept, not made again
    assert await _asked(OLD, key) is True            # ada is daniel's
    assert await _asked(SLOAN_MIND_KEY, key) is False  # dora is sloan's


async def test_the_console_presents_the_signed_in_owners_key_and_never_another_persons_to_a_mind(two_owners):
    token = mind_runtime._PERSON.set("daniel")
    try:
        ours = await mind_runtime.admin_headers(ADA_URL)
        daniel_key = ours["Authorization"].removeprefix("Bearer ")
        # Dora is sloan's: daniel's key is not sent there, and this console
        # holds no key for Dora, so nothing is sent at all.
        with pytest.raises(HTTPException) as refused:
            await mind_runtime.admin_headers(DORA_URL)
    finally:
        mind_runtime._PERSON.reset(token)

    assert refused.value.status_code == 503
    assert await _asked(OLD, daniel_key) is True
    assert await _asked(SLOAN_MIND_KEY, daniel_key) is False
    assert daniel_key != OLD


async def test_with_no_person_signed_in_a_mind_the_console_holds_is_still_changed_with_its_own_key(two_owners):
    headers = await mind_runtime.admin_headers(ADA_URL)

    assert headers == {"Authorization": f"Bearer {OLD}"}


async def test_replacing_a_users_key_refuses_the_old_one_at_the_next_question_and_accepts_the_new(two_owners):
    old = await holder_credentials.user_key("daniel")

    await holder_credentials.replace_user_key("daniel")

    new = credentials_data._read_env_value(two_owners.env, "USER_KEY_DANIEL")
    assert new and new != old
    assert await _asked(OLD, old) is False
    assert await _asked(OLD, new) is True


async def test_a_users_key_is_not_made_over_a_holder_that_is_not_a_users_key(two_owners):
    # `ada` is a mind at the gateway: a person called ada must not take it over.
    assert await holder_credentials.user_key("ada") is None
    assert await _asked(OLD, OLD) is False  # the mind's own key is not a user key either


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

    original = auth.resolve_user
    auth.resolve_user = resolved
    try:
        await mind_runtime.PersonScope(endpoint)({"type": "http", "headers": [], "path": "/x"}, None, None)
    finally:
        auth.resolve_user = original

    assert seen == ["daniel"]
    assert mind_runtime._PERSON.get() is None  # not left set after the request
