"""Each named operation writes what it was given, read back from outside.

Every assertion here reads the row with a plain driver connection rather
than through another operation. A round trip through one layer passes even
when every operation writes and reads the same wrong column consistently,
which is exactly the defect a round trip is supposed to catch.
"""

from __future__ import annotations

import uuid

import pytest

import hive_data
from hive_data import core


async def test_a_mind_registers_and_re_registers_without_being_refused(
        connected, clock, inspector):
    """`mind_server` re-registers from its own runtime.yaml on every start,
    which is what makes the file authoritative and the row a cache of it."""
    mind_id = f"mind-{uuid.uuid4().hex[:8]}"
    name = f"name-{mind_id}"
    await core.upsert_mind(
        mind_id=mind_id, name=name, gateway_url="http://127.0.0.1:8426",
        model="claude-opus-5", harness="claude", registered_at=clock,
        last_seen=clock, owner="daniel")

    again = await core.upsert_mind(
        mind_id=mind_id, name=name, gateway_url="http://127.0.0.1:8426",
        model="gpt-5.6-terra", harness="codex", registered_at=clock,
        last_seen=clock + 60)

    assert again["rows"] == 1
    row = await inspector.fetchrow(
        'SELECT "model", "harness", "last_seen", "owner" FROM core."minds" '
        'WHERE "name" = $1', name)
    assert row["model"] == "gpt-5.6-terra"
    assert row["harness"] == "codex"
    assert row["last_seen"] == clock + 60
    # The second registration named no owner, so the one it had stands.
    assert row["owner"] == "daniel"


async def test_a_session_is_written_with_every_field_it_was_given(
        connected, a_mind, clock, inspector):
    session_id = f"s-{uuid.uuid4().hex[:8]}"
    await core.create_session(
        id=session_id, surface="telegram", surface_id="chat-99",
        mind_id=a_mind, created_at=clock, last_active=clock,
        conversation_id="conv-1", model="claude-opus-5",
        opening_line="what is the plan", harness="claude", effort="high",
        name="the plan", color="silver", conversation_started_at=clock)

    row = await inspector.fetchrow(
        'SELECT * FROM core."sessions" WHERE "id" = $1', session_id)
    assert row["surface"] == "telegram"
    assert row["surface_id"] == "chat-99"
    assert row["conversation_id"] == "conv-1"
    assert row["opening_line"] == "what is the plan"
    assert row["harness"] == "claude"
    assert row["effort"] == "high"
    assert row["name"] == "the plan"
    assert row["color"] == "silver"
    assert row["status"] == "running"
    assert row["mind_id"] == a_mind


async def test_a_session_for_a_mind_that_does_not_exist_is_refused(
        connected, clock, inspector):
    """A foreign key that could never be declared while each store was its
    own SQLite file."""
    session_id = f"s-{uuid.uuid4().hex[:8]}"

    with pytest.raises(hive_data.MissingReference):
        await core.create_session(
            id=session_id, surface="telegram", surface_id="chat-1",
            mind_id="no-such-mind", created_at=clock, last_active=clock)

    assert await inspector.fetchval(
        'SELECT count(*) FROM core."sessions" WHERE "id" = $1',
        session_id) == 0


async def test_deleting_a_session_takes_its_turns_and_its_binding(
        connected, a_mind, clock, inspector):
    session_id = f"s-{uuid.uuid4().hex[:8]}"
    await core.create_session(
        id=session_id, surface="terminal", surface_id="tile-1",
        mind_id=a_mind, created_at=clock, last_active=clock)
    await core.record_turn(
        session_id=session_id, role="user", content="hi", created_at=clock)
    await core.bind_surface(
        client_type="terminal", client_ref="tile-1", session_id=session_id)

    assert (await core.delete_session(session_id))["rows"] == 1

    assert await inspector.fetchval(
        'SELECT count(*) FROM core."session_turns" WHERE "session_id" = $1',
        session_id) == 0
    assert await inspector.fetchval(
        'SELECT count(*) FROM core."active_sessions" WHERE "session_id" = $1',
        session_id) == 0


async def test_a_surface_binding_is_repointed_rather_than_refused(
        connected, a_mind, clock, inspector):
    """The primary key is the surface and its address, so a plain insert
    fails the first time somebody switches which session their chat is on."""
    first = f"s-{uuid.uuid4().hex[:8]}"
    second = f"s-{uuid.uuid4().hex[:8]}"
    for sid in (first, second):
        await core.create_session(
            id=sid, surface="telegram", surface_id="chat-1", mind_id=a_mind,
            created_at=clock, last_active=clock)

    await core.bind_surface(
        client_type="telegram", client_ref="chat-1", session_id=first)
    await core.bind_surface(
        client_type="telegram", client_ref="chat-1", session_id=second)

    rows = await inspector.fetch(
        'SELECT "session_id" FROM core."active_sessions" '
        'WHERE "client_type" = $1 AND "client_ref" = $2',
        "telegram", "chat-1")
    assert [r["session_id"] for r in rows] == [second]


async def test_a_duplicate_broker_message_is_benign_and_reports_no_insert(
        connected, clock, inspector):
    """`add_message` is idempotent on purpose: the SMS gateway re-POSTs the
    same webhook on any non-2xx, so a duplicate that raised would become a
    500 and be retried forever."""
    conversation = f"conv-{uuid.uuid4().hex[:8]}"
    await core.create_conversation(id=conversation, created_at=clock)
    body = dict(
        id=f"m-{uuid.uuid4().hex[:8]}", conversation_id=conversation,
        from_mind="skippy", to_mind="mordecai", message_number=1,
        content="the first one", status="delivered", timestamp=clock)

    first = await core.add_message(**body)
    again = await core.add_message(**body)

    assert first["rows"] == 1
    assert again["rows"] == 0
    assert await inspector.fetchval(
        'SELECT count(*) FROM core."messages" WHERE "conversation_id" = $1',
        conversation) == 1


async def test_a_message_for_a_conversation_that_does_not_exist_is_refused(
        connected, clock):
    """Distinct from the duplicate above: one is already done, the other
    names something that is not there, and they drive different code."""
    with pytest.raises(hive_data.MissingReference):
        await core.add_message(
            id=f"m-{uuid.uuid4().hex[:8]}", conversation_id="no-such-conv",
            from_mind="skippy", to_mind="mordecai", message_number=1,
            content="orphan", status="queued", timestamp=clock)


async def test_carried_forward_memory_is_read_by_mind_and_client_reference(
        connected, a_mind, clock, inspector):
    """Never by session: `conversation_id` on this table is a provenance
    stamp, which is what its old name hid."""
    await core.add_session_memory(
        mind_id=a_mind, mind_name="skippy", client_ref="chat-1",
        conversation_id="conv-old", body="the older summary",
        created_at=clock)
    newest = await core.add_session_memory(
        mind_id=a_mind, mind_name="skippy", client_ref="chat-1",
        conversation_id="conv-new", body="the newer summary",
        created_at=clock + 10)

    found = await core.latest_session_memory(
        mind_id=a_mind, client_ref="chat-1")

    assert found["body"] == "the newer summary"
    assert found["id"] == newest["row"]["id"]
    assert await inspector.fetchval(
        'SELECT count(*) FROM core."session_memory" WHERE "mind_id" = $1',
        a_mind) == 2


async def test_a_credential_is_upserted_and_then_revoked(
        connected, clock, inspector):
    holder = f"holder-{uuid.uuid4().hex[:8]}"
    await core.upsert_credential(
        holder=holder, username="scheduler", role="service",
        token_sha=f"sha-{uuid.uuid4().hex}", created_at=clock)

    assert (await core.revoke_credential(holder, clock + 5))["rows"] == 1
    assert await inspector.fetchval(
        'SELECT "revoked_at" FROM core."credentials" WHERE "holder" = $1',
        holder) == clock + 5


async def test_settings_scopes_people_and_overhead_round_trip(
        connected, a_mind, clock, inspector):
    key = f"key-{uuid.uuid4().hex[:8]}"
    await core.put_setting(key, "first", clock)
    await core.put_setting(key, "second", clock + 1)
    assert (await core.get_setting(key))["value"] == "second"

    await core.grant_secret_scope(
        mind_name="skippy", secret_key=key, granted_at=clock)
    assert [s["secret_key"] for s in
            await core.list_secret_scopes("skippy")] == [key]

    username = f"user-{uuid.uuid4().hex[:8]}"
    group = f"group-{uuid.uuid4().hex[:8]}"
    user = await core.create_user(
        username=username, password_hash="argon2:x", created_at=int(clock))
    await core.create_group(name=group, created_at=int(clock))
    await core.add_group_member(
        group_name=group, user_id=user["row"]["id"])
    assert [m["user_id"] for m in await core.list_group_members(group)] == [
        user["row"]["id"]]

    await core.record_overhead(
        mind_id=a_mind, harness="claude", model="claude-opus-5",
        surface="telegram", tokens=1234, observed_at=clock)
    await core.record_overhead(
        mind_id=a_mind, harness="claude", model="claude-opus-5",
        surface="telegram", tokens=4321, observed_at=clock + 1)
    assert (await core.get_overhead(
        mind_id=a_mind, harness="claude", model="claude-opus-5",
        surface="telegram"))["tokens"] == 4321
    assert await inspector.fetchval(
        'SELECT count(*) FROM core."context_overhead" WHERE "mind_id" = $1',
        a_mind) == 1
