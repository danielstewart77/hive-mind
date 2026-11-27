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


async def test_a_users_key_is_made_on_first_need_kept_and_accepted_only_for_minds_that_user_owns(two_owners):
    key = await holder_credentials.user_key("daniel")

    assert key and len(key) >= 32
    assert mind_key_files.read_person("daniel") == key
    # Not in the install's settings file, which a mind may be able to read.
    assert "USER_KEY_" not in two_owners.env.read_text()
    assert await holder_credentials.user_key("daniel") == key  # kept, not made again
    assert await _asked(OLD, key) is True            # ada is daniel's
    # Dora is sloan's. Daniel is the console's administrator, so his key may
    # change any mind; sloan, who is not, may change only dora.
    assert await _asked(SLOAN_MIND_KEY, key) is True
    sloan = await holder_credentials.user_key("sloan")
    assert await _asked(SLOAN_MIND_KEY, sloan) is True
    assert await _asked(OLD, sloan) is False


async def test_the_console_presents_the_signed_in_owners_key_through_the_gateway_and_never_another_persons(two_owners):
    token = mind_runtime._PERSON.set("daniel")
    try:
        url, headers = await mind_runtime.via_gateway(ADA_URL + "/runtime")
        daniel_key = headers["Authorization"].removeprefix("Bearer ")
        # Dora is sloan's and daniel is no administrator: nothing is sent.
        with pytest.raises(HTTPException) as refused:
            await mind_runtime.via_gateway(DORA_URL + "/runtime")
    finally:
        mind_runtime._PERSON.reset(token)

    assert url.endswith(f"/broker/minds/{ADA_ID}/forward/runtime")
    assert "ada.test" not in url
    assert refused.value.status_code == 403
    assert await _asked(OLD, daniel_key) is True
    assert daniel_key != OLD


async def test_with_no_person_signed_in_nothing_is_sent_to_a_mind(two_owners):
    token = mind_runtime._PERSON.set(None)
    try:
        with pytest.raises(HTTPException) as refused:
            await mind_runtime.via_gateway(ADA_URL + "/runtime")
    finally:
        mind_runtime._PERSON.reset(token)
    assert refused.value.status_code == 403


async def test_replacing_a_users_key_refuses_the_old_one_at_the_next_question_and_accepts_the_new(two_owners):
    old = await holder_credentials.user_key("daniel")

    await holder_credentials.replace_user_key("daniel")

    new = mind_key_files.read_person("daniel")
    assert new and new != old
    assert await _asked(OLD, old) is False
    assert await _asked(OLD, new) is True


async def test_a_users_key_is_not_made_over_a_holder_that_is_not_a_users_key(two_owners):
    # `ada` is a mind at the gateway: a person called ada must not take it over.
    assert await holder_credentials.user_key("ada") is None


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


async def test_a_key_still_in_the_install_settings_file_moves_to_the_key_folder_and_keeps_working(two_owners):
    legacy = "daniel-old-line-key-0123456789"
    line = holder_credentials.user_key_variable("daniel")
    two_owners.env.write_text(f"UNRELATED=keep\n{line}='{legacy}'\n")

    key = await holder_credentials.user_key("daniel")

    assert key == legacy
    assert mind_key_files.read_person("daniel") == legacy
    assert two_owners.env.read_text() == "UNRELATED=keep\n"
    assert await _asked(OLD, legacy) is True


async def test_a_replaced_key_is_never_left_in_the_settings_file_or_a_saved_copy_of_it(two_owners):
    legacy = "daniel-old-line-key-0123456789"
    line = holder_credentials.user_key_variable("daniel")
    two_owners.env.write_text(f"{line}={legacy}\n")
    credentials_data._backup(two_owners.env.read_text(), "hive_mind", line)

    await holder_credentials.replace_user_key("daniel")

    saved = "".join(p.read_text() for p in credentials_data.BACKUP_DIR.iterdir())
    assert legacy not in saved
    assert legacy not in two_owners.env.read_text()
    assert await _asked(OLD, legacy) is False


async def test_the_gateway_is_told_a_persons_key_by_the_database_not_by_its_administrator_credential(two_owners, monkeypatch):
    # With no gateway administrator credential anywhere, a key is still made
    # and accepted: the fingerprint is written to the hive database.
    monkeypatch.delenv("COMMS_ADMIN_BEARER_TOKEN", raising=False)

    key = await holder_credentials.user_key("daniel")

    assert key
    assert await _asked(OLD, key) is True


def test_who_is_an_administrator_is_in_the_hive_database_whichever_sign_in_the_site_uses(console):
    from hive_site import store

    admin = console.admin()
    sloan = console.settled_user(admin, "sloan", "sloan-password-1")

    async def admins():
        return await store.group_usernames("admins")

    assert sloan.get("/api/account/key").status_code == 200
    assert "sloan" not in testing.run(admins())
    assert "daniel" in testing.run(admins())

    assert admin.put("/api/admin/groups/admins/members/sloan").status_code in (200, 204)
    assert sloan.get("/api/account/key").status_code == 200
    assert "sloan" in testing.run(admins())

    assert admin.delete("/api/admin/groups/admins/members/sloan").status_code in (200, 204)
    assert sloan.get("/api/account/key").status_code == 200
    assert "sloan" not in testing.run(admins())


def test_an_administrator_replaces_anyones_key_and_anyone_else_only_their_own(console):
    admin = console.admin()
    sloan = console.settled_user(admin, "sloan", "sloan-password-1")
    console.settled_user(admin, "tess", "tess-password-1")

    assert sloan.post("/api/admin/users/tess/key/replace").status_code == 403
    own = sloan.post("/api/account/key/replace")
    assert own.status_code == 200 and own.json()["replaced"] is True
    assert mind_key_files.read_person("sloan")
    refused_for_missing = admin.post("/api/admin/users/nobody/key/replace")
    assert refused_for_missing.status_code == 404
    before = mind_key_files.read_person("tess")
    assert admin.post("/api/admin/users/tess/key/replace").status_code == 200
    assert mind_key_files.read_person("tess") not in (None, before)


async def test_a_crash_between_the_database_and_the_key_file_is_mended_by_the_next_ask(two_owners):
    key = await holder_credentials.user_key("daniel")
    # The database took a new key and the console died before the file did.
    await holder_credentials._record_user_key("daniel", "some-other-value-0123456789")
    assert await _asked(OLD, key) is False

    assert await holder_credentials.user_key("daniel") == key

    assert await _asked(OLD, key) is True


async def test_a_legacy_line_left_behind_beside_an_existing_key_file_is_removed_by_the_next_ask(two_owners):
    key = await holder_credentials.user_key("daniel")
    line = holder_credentials.user_key_variable("daniel")
    two_owners.env.write_text(f"UNRELATED=keep\n{line}=left-behind-0123456789\n")

    assert await holder_credentials.user_key("daniel") == key

    assert two_owners.env.read_text() == "UNRELATED=keep\n"


def test_two_people_whose_names_differ_only_in_letters_that_upper_case_alike_never_share_a_key_file():
    assert mind_key_files.person_stem("strasse") != mind_key_files.person_stem("straße")
    assert mind_key_files.person_stem("dan.s") != mind_key_files.person_stem("dan-s")


async def test_a_key_an_administrator_cut_off_is_not_brought_back_by_the_next_ask(two_owners):
    key = await holder_credentials.user_key("daniel")
    await holder_credentials._unrecord_user_key("daniel")

    assert await holder_credentials.user_key("daniel") is None
    assert await _asked(OLD, key) is False

    await holder_credentials.replace_user_key("daniel")
    assert await holder_credentials.user_key("daniel") != key
