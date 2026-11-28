"""A key made at one sign-in, which ends (feature 834, story 911).

Protected behaviours B19 (a sign-in key is accepted as that person and refused
once ended or past its time) and B22 (replacing a person's long-lived key ends
their sign-ins), against the real record on the scratch Postgres. The clock is
injected through `core.now`, the one place the record reads the time; nothing
sleeps. What was stored is read back on a plain driver connection.
"""

from __future__ import annotations

import uuid

import pytest

from hive_data import access, broker_ops, core

HOUR = 3600.0


def _name(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


@pytest.fixture
def now(monkeypatch, clock):
    """The record's clock, moved by the test."""
    moment = {"t": clock}
    monkeypatch.setattr(core, "now", lambda: moment["t"])
    return moment


async def _sign_in(person: str, key: str, now: dict, lasts: float = 12 * HOUR):
    return await core.record_sign_in(
        username=person, token_sha=access.digest(key),
        ends_at=now["t"] + lasts, created_at=now["t"])


async def test_a_sign_in_key_is_the_person_until_its_time_is_up_and_not_after(
        connected, now):
    person, key = _name("p-"), _name("key-")
    await _sign_in(person, key, now)

    found = await access.key_for(key)
    assert (found.username, found.holder, found.role) == (person, person, "user")

    now["t"] += 12 * HOUR - 1
    assert await access.key_for(key) is not None
    now["t"] += 1  # the very moment it ends
    assert await access.key_for(key) is None


async def test_a_sign_in_ended_early_is_refused_on_the_next_request(
        connected, now):
    person, key, other = _name("p-"), _name("key-"), _name("key-")
    await _sign_in(person, key, now)
    await _sign_in(person, other, now)

    await core.end_sign_in(access.digest(key), at=now["t"] + 5)
    now["t"] += 5

    assert await access.key_for(key) is None
    assert await access.key_for(other) is not None  # one device only


async def test_a_person_holds_several_sign_ins_and_ending_all_is_one_action(
        connected, now):
    person, bystander = _name("p-"), _name("p-")
    keys = [_name("key-") for _ in range(3)]
    for key in keys:
        await _sign_in(person, key, now)
    stays = _name("key-")
    await _sign_in(bystander, stays, now)
    for key in keys:
        assert (await access.key_for(key)).username == person

    ended = await core.end_sign_ins(person)

    assert ended["rows"] == 3
    for key in keys:
        assert await access.key_for(key) is None
    assert (await access.key_for(stays)).username == bystander


async def test_replacing_a_persons_long_lived_key_ends_their_sign_ins(
        connected, now):
    person, bystander = _name("p-"), _name("p-")
    old, new = _name("long-"), _name("long2-")
    await core.upsert_credential(
        holder=person, username=person, role="user",
        token_sha=access.digest(old), created_at=now["t"])
    signed_in, other_device = _name("key-"), _name("key-")
    await _sign_in(person, signed_in, now)
    await _sign_in(person, other_device, now)
    bystanders = _name("key-")
    await _sign_in(bystander, bystanders, now)

    await core.upsert_credential(
        holder=person, username=person, role="user",
        token_sha=access.digest(new), created_at=now["t"] + 1)

    assert await access.key_for(signed_in) is None
    assert await access.key_for(other_device) is None
    assert await access.key_for(old) is None
    assert (await access.key_for(new)).username == person
    assert (await access.key_for(bystanders)).username == bystander


async def test_cutting_off_a_persons_long_lived_key_ends_their_sign_ins(
        connected, now):
    person, long_key, signed_in = _name("p-"), _name("long-"), _name("key-")
    await core.upsert_credential(
        holder=person, username=person, role="user",
        token_sha=access.digest(long_key), created_at=now["t"])
    await _sign_in(person, signed_in, now)

    result = await broker_ops.revoke_live_credential(person, now["t"] + 1)
    now["t"] += 1

    assert result["rows"] == 1
    assert await access.key_for(long_key) is None
    assert await access.key_for(signed_in) is None


async def test_a_mind_replaced_keeps_every_persons_sign_in(connected, now):
    """Replacing a mind's key is not a person's key: no sign-in is ended."""
    person, signed_in, mind = _name("p-"), _name("key-"), _name("mind-")
    await _sign_in(person, signed_in, now)
    for token in (_name("k-"), _name("k-")):
        await core.upsert_credential(
            holder=mind, username=person, role="mind",
            token_sha=access.digest(token), created_at=now["t"])

    assert (await access.key_for(signed_in)).username == person


async def test_only_the_scramble_is_stored_and_the_key_is_not_listed_as_a_holder(
        connected, now, inspector):
    person, key = _name("p-"), _name("key-")
    await _sign_in(person, key, now)

    rows = await inspector.fetch(
        'SELECT * FROM core.credentials WHERE username = $1', person)
    assert len(rows) == 1
    assert rows[0]["token_sha"] == access.digest(key)
    assert key not in " ".join(str(v) for v in rows[0].values())
    assert rows[0]["ends_at"] == now["t"] + 12 * HOUR
    assert person not in [r["holder"] for r in await core.list_live_credentials()]
    assert person not in [r["holder"] for r in await broker_ops.list_credentials()]


async def test_ended_and_expired_sign_ins_are_cleared_out_and_live_ones_and_long_lived_keys_are_not(
        connected, now, inspector):
    person = _name("p-")
    ended, expired, live = _name("key-"), _name("key-"), _name("key-")
    await core.upsert_credential(
        holder=person, username=person, role="user",
        token_sha=access.digest(_name("long-")), created_at=now["t"],
        revoked_at=now["t"])  # a cut-off long-lived key stays on record
    await _sign_in(person, ended, now)
    await _sign_in(person, expired, now, lasts=HOUR)
    await _sign_in(person, live, now)
    await core.end_sign_in(access.digest(ended), at=now["t"] + 1)
    now["t"] += 2 * HOUR  # the expired one has run out

    cleared = await core.clear_ended_sign_ins()
    assert cleared["rows"] == 2

    left = await inspector.fetch(
        'SELECT token_sha, ends_at FROM core.credentials WHERE username = $1',
        person)
    shas = {r["token_sha"] for r in left}
    assert access.digest(live) in shas
    assert access.digest(ended) not in shas
    assert access.digest(expired) not in shas
    assert any(r["ends_at"] is None for r in left)  # the long-lived row



async def test_recording_a_sign_in_clears_the_ended_ones_so_the_record_does_not_grow(
        connected, now, inspector):
    person = _name("p-")
    old = _name("key-")
    await _sign_in(person, old, now, lasts=HOUR)
    now["t"] += 2 * HOUR

    fresh = _name("key-")
    await _sign_in(person, fresh, now)

    shas = {r["token_sha"] for r in await inspector.fetch(
        'SELECT token_sha FROM core.credentials WHERE username = $1', person)}
    assert shas == {access.digest(fresh)}
