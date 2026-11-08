"""The broker half of the gateway's operations, against a real database.

Each test names one thing a `broker_ops` operation decides and reads what it
produced back on a plain driver connection: a layer that writes and reads the
same wrong column passes its own round trip.
"""

from __future__ import annotations

import uuid

import pytest

from hive_data import broker_ops, core
from hive_data.errors import MissingReference, UnsettableColumn


def _name(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


async def _claim_row(inspector, mind_id: str, surface: str = "telegram"):
    return await inspector.fetchrow(
        'SELECT * FROM core."surface_claims" WHERE mind_id = $1 AND surface = $2',
        mind_id, surface)


# ------------------------------------------------------------------ messages

async def test_recovery_reads_one_status_oldest_first(connected, inspector, clock):
    conversation = _name("conv-")
    await core.create_conversation(id=conversation, created_at=clock)
    status = _name("st-")
    for number, (mid, at) in enumerate(
            [("b", clock + 2), ("a", clock + 1), ("c", clock + 3)], start=1):
        await core.add_message(
            id=f"{conversation}-{mid}", conversation_id=conversation,
            from_mind="x", to_mind="y", message_number=number, content="hi",
            status=status, timestamp=at)
    await core.add_message(
        id=f"{conversation}-other", conversation_id=conversation,
        from_mind="x", to_mind="y", message_number=9, content="hi",
        status="completed", timestamp=clock)

    rows = await broker_ops.messages_with_status(status)

    assert [r["id"] for r in rows] == [
        f"{conversation}-a", f"{conversation}-b", f"{conversation}-c"]


async def test_moving_messages_records_the_reason_and_counts_them(
        connected, inspector, clock):
    conversation = _name("conv-")
    was = _name("st-")
    await core.create_conversation(id=conversation, created_at=clock)
    for number in (1, 2):
        await core.add_message(
            id=f"{conversation}-{number}", conversation_id=conversation,
            from_mind="x", to_mind="y", message_number=number, content="hi",
            status=was, timestamp=clock)

    moved = await broker_ops.move_messages(
        was=was, now="failed", response_error="server_restart_during_delivery")

    assert moved["rows"] == 2
    rows = await inspector.fetch(
        'SELECT status, response_error FROM core."messages" '
        'WHERE conversation_id = $1', conversation)
    assert {(r["status"], r["response_error"]) for r in rows} == {
        ("failed", "server_restart_during_delivery")}


async def test_a_message_reads_back_by_id(connected, clock):
    conversation = _name("conv-")
    await core.create_conversation(id=conversation, created_at=clock)
    await core.add_message(
        id=f"{conversation}-1", conversation_id=conversation, from_mind="x",
        to_mind="y", message_number=1, content="the body", status="pending",
        timestamp=clock)

    row = await broker_ops.get_message(f"{conversation}-1")

    assert row["content"] == "the body"
    assert await broker_ops.get_message(_name("nope-")) is None


# --------------------------------------------------------------------- minds

async def test_updating_a_mind_never_resurrects_a_deleted_one(
        connected, inspector):
    mind_id = _name("gone-")

    result = await broker_ops.update_mind(mind_id, model="m2")

    assert result["rows"] == 0
    assert await inspector.fetchval(
        'SELECT count(*) FROM core."minds" WHERE mind_id = $1', mind_id) == 0


async def test_updating_a_mind_sets_only_what_was_passed(
        connected, inspector, a_mind):
    """`None` clears a column; a column not named is left alone."""
    await inspector.execute(
        'UPDATE core."minds" SET voice = $2 WHERE mind_id = $1',
        a_mind, "am_adam")

    await broker_ops.update_mind(a_mind, owner="daniel", voice=None)

    row = await inspector.fetchrow(
        'SELECT owner, voice, model FROM core."minds" WHERE mind_id = $1',
        a_mind)
    assert row["owner"] == "daniel"
    assert row["voice"] is None
    assert row["model"] == "claude-opus-5"


@pytest.mark.parametrize("column", ["mind_id", "registered_at"])
async def test_a_minds_identity_and_first_registration_are_not_settable(
        connected, a_mind, column):
    with pytest.raises(UnsettableColumn):
        await broker_ops.update_mind(a_mind, **{column: "x"})


async def test_a_mind_with_conversations_refuses_deletion(
        connected, inspector, a_mind, clock):
    for _ in range(2):
        await core.create_session(
            id=_name("s-"), surface="telegram", surface_id="1",
            mind_id=a_mind, created_at=clock, last_active=clock)

    assert await broker_ops.count_mind_sessions(a_mind) == 2
    with pytest.raises(MissingReference):
        await broker_ops.delete_mind(a_mind)
    assert await inspector.fetchval(
        'SELECT count(*) FROM core."minds" WHERE mind_id = $1', a_mind) == 1


async def test_deleting_a_mind_takes_its_claims_with_it(
        connected, inspector, a_mind):
    await core.upsert_claim(mind_id=a_mind, surface="telegram", bot_ref="b")

    result = await broker_ops.delete_mind(a_mind)

    assert result["rows"] == 1
    assert await _claim_row(inspector, a_mind) is None


async def test_revoking_every_scope_of_a_name_leaves_other_names_alone(
        connected, inspector, clock):
    mine, theirs = _name("m-"), _name("m-")
    for name in (mine, theirs):
        for key in ("A", "B"):
            await core.grant_secret_scope(
                mind_name=name, secret_key=key, granted_at=clock)

    result = await broker_ops.revoke_all_secret_scopes(mine)

    assert result["rows"] == 2
    rows = await inspector.fetch(
        'SELECT mind_name FROM core."secret_scopes" '
        'WHERE mind_name = ANY($1::text[])', [mine, theirs])
    assert {r["mind_name"] for r in rows} == {theirs}


# --------------------------------------------------------------- credentials

async def test_a_revoked_holders_digest_still_names_it(connected, clock):
    holder, sha = _name("h-"), _name("sha-")
    await core.upsert_credential(
        holder=holder, username="u", role="person", token_sha=sha,
        created_at=clock, revoked_at=clock)

    assert await broker_ops.holder_of_digest(sha) == holder
    assert await broker_ops.holder_of_digest(_name("sha-")) is None


async def test_a_second_revoke_changes_nothing(connected, inspector, clock):
    holder = _name("h-")
    await core.upsert_credential(
        holder=holder, username="u", role="person", token_sha=_name("sha-"),
        created_at=clock)

    first = await broker_ops.revoke_live_credential(holder, clock + 1)
    second = await broker_ops.revoke_live_credential(holder, clock + 2)

    assert (first["rows"], second["rows"]) == (1, 0)
    assert await inspector.fetchval(
        'SELECT revoked_at FROM core."credentials" WHERE holder = $1',
        holder) == clock + 1


async def test_the_credential_listing_keeps_revoked_rows_and_no_digest(
        connected, clock):
    holder = _name("h-")
    await core.upsert_credential(
        holder=holder, username="u", role="person", token_sha=_name("sha-"),
        created_at=clock, revoked_at=clock)

    rows = [r for r in await broker_ops.list_credentials()
            if r["holder"] == holder]

    assert len(rows) == 1
    assert rows[0]["revoked_at"] == clock
    assert "token_sha" not in rows[0]


# ------------------------------------------------------------ surface claims

async def test_releasing_an_unregistered_bot_writes_no_row(
        connected, inspector, a_mind, clock):
    result = await broker_ops.update_claim(
        a_mind, "telegram", claimed_ref=None, released_at=clock)

    assert result["rows"] == 0
    assert await _claim_row(inspector, a_mind) is None


async def test_a_phrase_is_never_offered_on_a_claimed_bot(
        connected, inspector, a_mind, clock):
    await core.upsert_claim(
        mind_id=a_mind, surface="telegram", claimed_ref="42",
        claimed_at=clock)

    result = await broker_ops.offer_claim_phrase(
        mind_id=a_mind, surface="telegram", phrase_hash="h",
        issued_at=clock, expires_at=clock + 60)

    assert result["rows"] == 0
    row = await _claim_row(inspector, a_mind)
    assert row["phrase_hash"] is None and row["claimed_ref"] == "42"


async def test_a_new_phrase_replaces_the_old_and_resets_the_guesses(
        connected, inspector, a_mind, clock):
    await core.upsert_claim(
        mind_id=a_mind, surface="telegram", phrase_hash="old",
        phrase_attempts=5)

    result = await broker_ops.offer_claim_phrase(
        mind_id=a_mind, surface="telegram", phrase_hash="new",
        issued_at=clock, expires_at=clock + 60)

    assert result["rows"] == 1
    row = await _claim_row(inspector, a_mind)
    assert (row["phrase_hash"], row["phrase_attempts"]) == ("new", 0)


async def test_a_bot_with_only_a_saved_token_is_adopted(
        connected, inspector, a_mind, clock):
    await core.upsert_claim(mind_id=a_mind, surface="telegram", bot_ref="b")

    result = await broker_ops.adopt_claim_if_undecided(
        mind_id=a_mind, surface="telegram", claimed_ref="42",
        claimed_at=clock, now=clock)

    assert result["rows"] == 1
    assert (await _claim_row(inspector, a_mind))["claimed_ref"] == "42"


@pytest.mark.parametrize("decided", [
    {"released_at": 1.0},
    {"phrase_hash": "h", "phrase_expires_at": 1_800_000_060.0},
    {"phrase_hash": "h", "phrase_expires_at": None},
])
async def test_a_decided_bot_is_not_adopted(
        connected, inspector, a_mind, clock, decided):
    await core.upsert_claim(mind_id=a_mind, surface="telegram", **decided)

    result = await broker_ops.adopt_claim_if_undecided(
        mind_id=a_mind, surface="telegram", claimed_ref="42",
        claimed_at=clock, now=clock)

    assert result["rows"] == 0
    assert (await _claim_row(inspector, a_mind))["claimed_ref"] is None


async def test_a_bot_whose_phrase_expired_unsent_is_adopted(
        connected, inspector, a_mind, clock):
    await core.upsert_claim(
        mind_id=a_mind, surface="telegram", phrase_hash="h",
        phrase_expires_at=clock - 1)

    result = await broker_ops.adopt_claim_if_undecided(
        mind_id=a_mind, surface="telegram", claimed_ref="42",
        claimed_at=clock, now=clock)

    assert result["rows"] == 1
    row = await _claim_row(inspector, a_mind)
    assert (row["claimed_ref"], row["phrase_hash"]) == ("42", None)

