"""The behaviours hive-tools depends on, through the `tools` operations.

Every assertion reads the row back with a plain driver connection rather than
through a second operation, for the reason the round-trip suite gives: a
layer that writes and reads the same wrong column consistently passes its
own round trip.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

import hive_data

from tests._scratch import open_claimed
from tests.conftest import ADMIN_DSN
from hive_data import tools


def _id() -> str:
    return f"req-{uuid.uuid4().hex[:10]}"


def _tool() -> str:
    """A tool name of this test's own.

    The scratch database is made once per run, so rows outlive the test that
    wrote them. A shared name here would have one test reading another's
    settings row and calling it a leak.
    """
    return f"tool-{uuid.uuid4().hex[:10]}"


async def test_every_tools_table_returns_what_was_written(
        connected, inspector):
    """Requirement one, and the only thing that proves a table is actually
    reachable: a value put in through the layer comes back out of it."""
    at = 1_800_000_000
    request_id = _id()
    await tools.create_request(
        request_id, "gmail.send", "skippy", '{"to": "daniel"}', at, at + 600,
        tool_method="POST", tool_path="/gmail/send", tool_body="{}")
    await tools.grant_sudo("gmail.send", "skippy", at, at + 900)
    setting_tool = _tool()
    await tools.set_hitl_setting(setting_tool, "ask", 30)
    digest = f"sha-{uuid.uuid4().hex}"
    username = f"user-{uuid.uuid4().hex[:8]}"
    token = await tools.create_token("console", digest, at)
    user = await tools.create_user(username, "argon2-hash", 1, at)

    found = await tools.request(request_id)
    assert found is not None
    assert found["tool_name"] == "gmail.send"
    assert found["caller_mind"] == "skippy"
    assert found["tool_method"] == "POST"
    assert found["tool_path"] == "/gmail/send"
    assert found["status"] == "pending"
    # The captured body is private, so the request read does not carry it;
    # `request_payload` is the one that names it.
    payload = await tools.request_payload(request_id)
    assert payload is not None
    assert payload["params_json"] == '{"to": "daniel"}'

    grant = await tools.active_grant("gmail.send", "skippy", at)
    assert grant is not None and grant["expires_at"] == at + 900

    setting = await tools.hitl_setting(setting_tool)
    assert setting == {"tool_name": setting_tool, "mode": "ask",
                       "sudo_timeout_minutes": 30}

    live = await tools.live_token(digest)
    assert live is not None and live["name"] == "console"
    assert live["id"] == token["row"]["id"]

    login = await tools.user(username)
    assert login is not None and login["password_hash"] == "argon2-hash"
    assert login["id"] == user["row"]["id"]

    # Read from outside the package as well, so a consistently wrong column
    # cannot carry every assertion above.
    row = await inspector.fetchrow(
        'SELECT tool_name, status FROM tools."hitl_requests" WHERE id = $1',
        request_id)
    assert dict(row) == {"tool_name": "gmail.send", "status": "pending"}


async def test_the_queue_is_newest_first_and_capped(connected):
    """What the approvals page renders. Reversing the order or widening the
    cap in `recent_requests` fails here."""
    at = 1_800_000_000
    made = []
    for n in range(53):
        request_id = _id()
        made.append(request_id)
        await tools.create_request(
            request_id, "docker.restart", "skippy", "{}", at + n,
            at + n + 600)

    queue = await tools.recent_requests()
    assert len(queue) == 50
    assert [r["id"] for r in queue] == list(reversed(made))[:50]
    assert [r["created_at"] for r in queue] == sorted(
        (r["created_at"] for r in queue), reverse=True)


async def test_a_request_that_is_not_there_is_nothing_rather_than_a_blank_row(
        connected):
    """A caller cannot tell an empty dict from a row whose every column came
    back empty, so the absent answer is None."""
    assert await tools.request(_id()) is None
    assert await tools.pending_request(_id(), 1_800_000_000) is None
    assert await tools.hitl_setting(_tool()) is None
    assert await tools.active_grant(_tool(), "nobody", 1_800_000_000) is None
    assert await tools.user(f"nobody-{uuid.uuid4().hex[:8]}") is None
    assert await tools.user_by_id(-1) is None


async def test_a_request_past_its_expiry_is_not_offered_as_pending(
        connected):
    """The boundary, driven by a time the test hands in rather than the wall
    clock. A request expiring exactly now is still decidable; one that
    expired a second ago is not."""
    at = 1_800_000_000
    expires_at = at + 600
    request_id = _id()
    await tools.create_request(
        request_id, "gmail.send", "skippy", "{}", at, expires_at)

    assert await tools.pending_request(request_id, expires_at - 1) is not None
    assert await tools.pending_request(request_id, expires_at) is not None
    assert await tools.pending_request(request_id, expires_at + 1) is None
    # Still a request, just not a decidable one.
    assert await tools.request(request_id) is not None


async def test_a_request_is_decided_once_and_only_once(connected, inspector):
    """Two people reaching the queue at the same moment produce one decision.
    Dropping the pending guard from the statement fails here."""
    at = 1_800_000_000
    request_id = _id()
    await tools.create_request(
        request_id, "gmail.send", "skippy", "{}", at, at + 600)

    first = await tools.decide_request(request_id, "approved", at + 5, at + 5)
    assert first["rows"] == 1

    second = await tools.decide_request(request_id, "denied", at + 6, at + 6)
    assert second["rows"] == 0

    row = await inspector.fetchrow(
        'SELECT status, decided_at FROM tools."hitl_requests" '
        "WHERE id = $1", request_id)
    assert dict(row) == {"status": "approved", "decided_at": at + 5}


async def test_an_expired_request_cannot_be_decided(connected):
    """The expiry rule lives in the same statement as the decision, so a
    request cannot expire between being read and being approved."""
    at = 1_800_000_000
    request_id = _id()
    await tools.create_request(
        request_id, "gmail.send", "skippy", "{}", at, at + 600)

    late = await tools.decide_request(
        request_id, "approved", at + 601, at + 601)
    assert late["rows"] == 0


async def test_work_in_one_transaction_is_undone_together(
        connected, inspector):
    """The test that fails if an operation ignores the unit it was handed and
    opens a connection of its own: nothing here is committed."""
    at = 1_800_000_000
    request_id = _id()
    tool_name = _tool()

    class Deliberate(Exception):
        pass

    with pytest.raises(Deliberate):
        async with hive_data.transaction() as unit:
            await tools.create_request(
                request_id, "gmail.send", "skippy", "{}", at, at + 600,
                unit=unit)
            await tools.set_hitl_setting(
                tool_name, "never", 5, unit=unit)
            raise Deliberate

    assert await inspector.fetchval(
        'SELECT count(*) FROM tools."hitl_requests" WHERE id = $1',
        request_id) == 0
    assert await inspector.fetchval(
        'SELECT count(*) FROM tools."tool_hitl_settings" '
        "WHERE tool_name = $1", tool_name) == 0


async def test_a_revoked_token_stops_authenticating(connected, inspector):
    """The whole point of revoking one. `revoked_at = NULL` is never true, so
    a guard spelled as an ordinary condition matches no row, reports zero,
    and leaves the bearer working."""
    at = 1_800_000_000
    digest = f"sha-{uuid.uuid4().hex}"
    token = await tools.create_token("console", digest, at)
    token_id = token["row"]["id"]

    assert await tools.live_token(digest) is not None

    revoked = await tools.revoke_token(token_id, at + 10)
    assert revoked["rows"] == 1
    assert await inspector.fetchval(
        'SELECT revoked_at FROM tools."api_tokens" WHERE id = $1',
        token_id) == at + 10
    assert await tools.live_token(digest) is None

    # Revoking it again is the no-op the row count has to report honestly,
    # because the caller reads that count as "somebody else got there first".
    again = await tools.revoke_token(token_id, at + 20)
    assert again["rows"] == 0
    assert await inspector.fetchval(
        'SELECT revoked_at FROM tools."api_tokens" WHERE id = $1',
        token_id) == at + 10


async def test_a_token_use_is_recorded_against_that_token(
        connected, inspector):
    at = 1_800_000_000
    one = await tools.create_token("a", f"sha-{uuid.uuid4().hex}", at)
    two = await tools.create_token("b", f"sha-{uuid.uuid4().hex}", at)

    used = await tools.record_token_use(one["row"]["id"], at + 7)
    assert used["rows"] == 1
    assert await inspector.fetchval(
        'SELECT last_used_at FROM tools."api_tokens" WHERE id = $1',
        one["row"]["id"]) == at + 7
    assert await inspector.fetchval(
        'SELECT last_used_at FROM tools."api_tokens" WHERE id = $1',
        two["row"]["id"]) is None


async def test_a_grant_belongs_to_the_mind_it_was_given_to(connected):
    """A permission table, and this is the only filter that scopes it: one
    mind's sudo grant must not authorize another."""
    at = 1_800_000_000
    tool_name = _tool()
    await tools.grant_sudo(tool_name, "skippy", at, at + 900)

    assert await tools.active_grant(tool_name, "skippy", at) is not None
    assert await tools.active_grant(tool_name, "mordecai", at) is None
    assert await tools.active_grant(_tool(), "skippy", at) is None


async def test_a_grant_expires_at_its_expiry_rather_than_after_it(connected):
    """Strictly after now, which is what hive-tools asks today: a grant whose
    expiry is this instant has run out."""
    at = 1_800_000_000
    expires_at = at + 900
    tool_name = _tool()
    await tools.grant_sudo(tool_name, "skippy", at, expires_at)

    assert await tools.active_grant(
        tool_name, "skippy", expires_at - 1) is not None
    assert await tools.active_grant(tool_name, "skippy", expires_at) is None


async def test_the_decision_and_the_queue_agree_on_the_instant_of_expiry(
        connected):
    """The read that offers a request and the write that decides it share one
    boundary. If they disagree, a request the queue offers cannot be decided
    and the operator is told it was already resolved."""
    at = 1_800_000_000
    expires_at = at + 600
    request_id = _id()
    await tools.create_request(
        request_id, "gmail.send", "skippy", "{}", at, expires_at)

    assert await tools.pending_request(request_id, expires_at) is not None
    decided = await tools.decide_request(
        request_id, "approved", expires_at, expires_at)
    assert decided["rows"] == 1


async def test_a_decided_request_is_no_longer_waiting_on_anybody(connected):
    """`pending_request` means still waiting on a person. Dropping its status
    guard reports an approved request as pending for ever."""
    at = 1_800_000_000
    request_id = _id()
    await tools.create_request(
        request_id, "gmail.send", "skippy", "{}", at, at + 600)
    await tools.decide_request(request_id, "denied", at + 1, at + 1)

    assert await tools.pending_request(request_id, at + 2) is None


async def test_deciding_a_request_hands_back_the_call_to_replay(connected):
    """The winner replays the call it approved, and re-reading it afterwards
    is a statement the loser runs too."""
    at = 1_800_000_000
    request_id = _id()
    await tools.create_request(
        request_id, "docker.restart", "skippy", '{"name": "hive-tools"}',
        at, at + 600, tool_method="POST", tool_path="/docker/restart",
        tool_body='{"name": "hive-tools"}')

    decided = await tools.decide_request(request_id, "approved", at + 1,
                                         at + 1)
    assert decided["rows"] == 1
    assert decided["row"]["tool_method"] == "POST"
    assert decided["row"]["tool_path"] == "/docker/restart"
    assert decided["row"]["tool_body"] == '{"name": "hive-tools"}'


async def test_a_request_cannot_be_decided_back_to_pending(connected):
    at = 1_800_000_000
    request_id = _id()
    await tools.create_request(
        request_id, "gmail.send", "skippy", "{}", at, at + 600)

    with pytest.raises(hive_data.ValueRefused):
        await tools.decide_request(request_id, "pending", at + 1, at + 1)


async def test_only_a_waiting_request_can_be_given_longer(
        connected, inspector):
    at = 1_800_000_000
    waiting, settled = _id(), _id()
    await tools.create_request(
        waiting, "gmail.send", "skippy", "{}", at, at + 600)
    await tools.create_request(
        settled, "gmail.send", "skippy", "{}", at, at + 600)
    await tools.decide_request(settled, "denied", at + 1, at + 1)

    assert (await tools.extend_request(waiting, at + 9000))["rows"] == 1
    assert (await tools.extend_request(settled, at + 9000))["rows"] == 0
    assert await inspector.fetchval(
        'SELECT expires_at FROM tools."hitl_requests" WHERE id = $1',
        settled) == at + 600


async def test_seeding_a_mode_leaves_an_edited_one_alone(
        connected, inspector):
    """hive-tools registers its tools on every boot. An operator who turned a
    gate off expects it to stay off across a restart, so the seed is
    insert-if-absent and the replacing write is a different operation."""
    tool_name = _tool()
    first = await tools.seed_hitl_setting(tool_name, "on", 15)
    assert first["rows"] == 1

    await tools.set_hitl_setting(tool_name, "off", 15)
    again = await tools.seed_hitl_setting(tool_name, "on", 15)
    assert again["rows"] == 0
    assert await inspector.fetchval(
        'SELECT mode FROM tools."tool_hitl_settings" WHERE tool_name = $1',
        tool_name) == "off"


async def test_setting_a_mode_replaces_the_one_that_was_there(
        connected, inspector):
    tool_name = _tool()
    await tools.seed_hitl_setting(tool_name, "on", 15)
    await tools.set_hitl_setting(tool_name, "off", 45)

    row = await inspector.fetchrow(
        'SELECT mode, sudo_timeout_minutes FROM tools."tool_hitl_settings" '
        "WHERE tool_name = $1", tool_name)
    assert dict(row) == {"mode": "off", "sudo_timeout_minutes": 45}


async def test_the_settings_page_reads_every_mode_by_tool_name(connected):
    names = sorted(_tool() for _ in range(3))
    for name in names:
        await tools.seed_hitl_setting(name, "on", 15)

    listed = [r["tool_name"] for r in await tools.hitl_settings()]
    assert [n for n in listed if n in names] == names


async def test_no_listing_hands_back_a_secret_or_a_captured_body(connected):
    """Reading a digest or a hash is how one caller authenticates as another,
    and the queue's bodies are what every gated call was called with."""
    at = 1_800_000_000
    await tools.create_token("console", f"sha-{uuid.uuid4().hex}", at)
    await tools.create_user(f"user-{uuid.uuid4().hex[:8]}", "argon2", 1, at)
    await tools.create_request(
        _id(), "gmail.send", "skippy", '{"to": "daniel"}', at, at + 600,
        tool_body='{"secret": "do not list me"}')

    for row in await tools.tokens():
        assert "token_hash" not in row
    for row in await tools.users():
        assert "password_hash" not in row
    for row in await tools.recent_requests():
        assert "params_json" not in row
        assert "tool_body" not in row


async def test_the_replay_read_is_the_one_that_names_the_body(connected):
    at = 1_800_000_000
    request_id = _id()
    await tools.create_request(
        request_id, "gmail.send", "skippy", '{"to": "daniel"}', at, at + 600,
        tool_body='{"subject": "hello"}')

    payload = await tools.request_payload(request_id)
    assert payload is not None
    assert payload["params_json"] == '{"to": "daniel"}'
    assert payload["tool_body"] == '{"subject": "hello"}'


async def test_a_queue_limit_that_is_not_a_cap_is_refused(connected):
    """`LIMIT NULL` is no limit, and this listing is the whole table when it
    is not capped."""
    with pytest.raises(hive_data.ValueRefused):
        await tools.recent_requests(limit=None)
    with pytest.raises(hive_data.ValueRefused):
        await tools.recent_requests(limit=0)


async def test_a_login_is_found_by_the_id_a_session_cookie_carries(connected):
    at = 1_800_000_000
    username = f"user-{uuid.uuid4().hex[:8]}"
    created = await tools.create_user(username, "argon2-hash", 0, at)

    by_id = await tools.user_by_id(created["row"]["id"])
    assert by_id is not None
    assert by_id["username"] == username
    assert by_id["password_hash"] == "argon2-hash"


async def test_disabling_a_login_marks_that_login(connected, inspector):
    at = 1_800_000_000
    one = f"user-{uuid.uuid4().hex[:8]}"
    two = f"user-{uuid.uuid4().hex[:8]}"
    await tools.create_user(one, "argon2", 0, at)
    await tools.create_user(two, "argon2", 0, at)

    assert (await tools.disable_user(one, at + 5))["rows"] == 1
    assert await inspector.fetchval(
        'SELECT disabled_at FROM tools."users" WHERE username = $1',
        one) == at + 5
    assert await inspector.fetchval(
        'SELECT disabled_at FROM tools."users" WHERE username = $1',
        two) is None


async def test_an_identity_column_cannot_be_written_through_a_dotted_key(
        connected):
    """Postgres takes a value on a GENERATED BY DEFAULT column without
    advancing the sequence, so the row lands and the next insert collides."""
    sql_module = __import__("hive_data._sql", fromlist=["insert"])
    with pytest.raises(hive_data.IdentityNotWritable) as refusal:
        sql_module.insert("tools.users", {"id": 1, "username": "x"})
    assert "tools.users.id" in str(refusal.value)


async def test_a_tools_schema_with_no_tables_does_not_stop_everyone_else(
        scratch_dsn, monkeypatch):
    """Every service in the hive shares one database. A migration that made
    the schema and not the tables would otherwise stop comms, lucent and
    every mind from opening a pool at all, over a context none of them use.
    `CREATE SCHEMA` on its own is not a context."""

    scratch = await open_claimed(ADMIN_DSN, "hive_data_other_")
    dsn = scratch.dsn
    target = await asyncpg.connect(dsn)
    try:
        from tests.conftest import SCHEMA_SQL
        await target.execute(SCHEMA_SQL.read_text())
        await target.execute("CREATE SCHEMA tools")
    finally:
        await target.close()

    monkeypatch.setenv(hive_data.config.DSN_VAR, dsn)
    try:
        await hive_data.connect()
    finally:
        await hive_data.close(drain_timeout=5.0)
        await scratch.close()


async def test_a_tools_schema_missing_one_table_still_refuses(
        scratch_dsn, monkeypatch):
    """Half a context is drift, not an absent context, and it is named."""

    scratch = await open_claimed(ADMIN_DSN, "hive_data_other_")
    dsn = scratch.dsn
    target = await asyncpg.connect(dsn)
    try:
        from tests.conftest import SCHEMA_SQL, TOOLS_SCHEMA_SQL
        await target.execute(SCHEMA_SQL.read_text())
        await target.execute(TOOLS_SCHEMA_SQL.read_text())
        await target.execute('DROP TABLE tools."sudo_grants"')
    finally:
        await target.close()

    monkeypatch.setenv(hive_data.config.DSN_VAR, dsn)
    try:
        with pytest.raises(hive_data.SchemaMismatch) as refusal:
            await hive_data.connect()
        assert "tools.sudo_grants" in str(refusal.value)
    finally:
        await hive_data.close(drain_timeout=5.0)
        await scratch.close()


async def test_the_login_count_counts_logins(connected, inspector):
    """Asked at every boot to decide whether the bootstrap admin is needed.
    The count is the answer, not the length of a listing that carried every
    password hash over the wire to produce it."""
    before = await tools.count_users()
    at = 1_800_000_000
    await tools.create_user(f"user-{uuid.uuid4().hex[:8]}", "hash", 0, at)
    await tools.create_user(f"user-{uuid.uuid4().hex[:8]}", "hash", 0, at)

    assert await tools.count_users() == before + 2
    assert await tools.count_users() == await inspector.fetchval(
        'SELECT count(*) FROM tools."users"')
