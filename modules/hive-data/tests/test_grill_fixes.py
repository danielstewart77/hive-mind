"""The behaviours the first pass had no test for, and the ones it got wrong.

Each of these names a mutation that left the suite green, or a defect the
code grill reproduced against a running database.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import asyncpg
import pytest

import hive_data

from tests._scratch import open_claimed
from tests.conftest import ADMIN_DSN
from hive_data import core
from hive_data.pool import translate


# ---------------------------------------------------------------------------
# The taxonomy, as a pure function
# ---------------------------------------------------------------------------

def test_an_exception_this_package_does_not_know_is_re_raised_unchanged():
    """A bug dressed as a domain error is a bug wearing the costume of an
    outcome the caller has a branch for, and the branch will swallow it."""
    with pytest.raises(NameError):
        translate(NameError("a real bug"))


def test_a_host_that_never_answers_is_unreachable_not_a_timeout():
    """`TimeoutError` is `asyncio.TimeoutError` and a subclass of `OSError`,
    so the class cannot say which it was. A dead host reported as a timeout
    asserts the database accepted work it never received."""
    assert isinstance(
        translate(TimeoutError(), connecting=True), hive_data.Unreachable)
    assert isinstance(translate(TimeoutError()), hive_data.TimedOut)


def test_a_server_that_went_away_is_not_a_refused_write():
    """A restart, an administrative shutdown and a connection closed
    mid-statement are the server going away, and the remedy is nothing to do
    with the statement being reported."""
    for exc in (asyncpg.ConnectionDoesNotExistError("gone"),
                asyncpg.AdminShutdownError("shutting down"),
                asyncpg.CrashShutdownError("crashed")):
        assert isinstance(translate(exc), hive_data.Unreachable), exc


def test_a_connection_the_driver_cannot_continue_from_is_the_server_gone():
    """`InternalClientError` derives straight from `Exception`, so it misses
    the `InterfaceError`, `PostgresError` and `OSError` branches alike and
    reaches the re-raise reserved for bugs in our own code. Terminating every
    backend under a live pool raises it once per pooled connection, and the
    caller was being handed `cannot switch to state 11; another operation is
    in progress` - which reads as a concurrency bug in its own store."""
    translated = translate(
        asyncpg.InternalClientError(
            "cannot switch to state 11; another operation (2) is in "
            "progress"))
    assert isinstance(translated, hive_data.Unreachable)


def test_a_server_out_of_connections_is_not_unreachable():
    """It is up and answering everybody else."""
    assert isinstance(
        translate(asyncpg.TooManyConnectionsError("full")),
        hive_data.NoConnectionAvailable)


def test_an_aborted_transaction_is_its_own_outcome():
    """Every statement after a handled failure is refused without being
    read, and calling that a rejected write blames a write nobody looked at
    while the real cause has already been caught."""
    assert isinstance(
        translate(asyncpg.InFailedSQLTransactionError("aborted")),
        hive_data.TransactionAborted)


def test_a_uniqueness_violation_is_a_conflict_and_a_missing_parent_is_not():
    assert isinstance(
        translate(asyncpg.UniqueViolationError("dup")), hive_data.Conflict)
    assert isinstance(
        translate(asyncpg.ForeignKeyViolationError("fk")),
        hive_data.MissingReference)
    assert not isinstance(
        translate(asyncpg.ForeignKeyViolationError("fk")), hive_data.Conflict)


def test_a_pool_whose_maximum_is_below_its_minimum_is_refused():
    with pytest.raises(hive_data.NotConfigured):
        hive_data.Settings(dsn="postgresql://h/x", pool_min=5, pool_max=2)


# ---------------------------------------------------------------------------
# Inside a unit, the taxonomy still applies
# ---------------------------------------------------------------------------

async def test_an_operation_inside_a_unit_raises_this_packages_errors(
        connected, clock):
    """Translating only at the transaction boundary means `except Conflict`
    around an operation passed `unit=` does not fire — the exception escapes
    as a raw driver class and the handler above turns a duplicate into a
    500, which is the retry loop the taxonomy exists to prevent."""
    seen: Exception | None = None
    with pytest.raises(hive_data.MissingReference):
        async with hive_data.transaction() as unit:
            try:
                await core.add_message(
                    id=f"m-{uuid.uuid4().hex[:8]}",
                    conversation_id="no-such-conversation",
                    from_mind="skippy", to_mind="mordecai", message_number=1,
                    content="orphan", status="queued", timestamp=clock,
                    unit=unit)
            except Exception as exc:  # noqa: BLE001 - that is the point
                seen = exc
                raise

    assert isinstance(seen, hive_data.MissingReference), type(seen)


async def test_an_unguarded_duplicate_inside_a_unit_is_a_conflict(
        connected, a_mind, clock):
    """Nothing in the first pass provoked an unguarded uniqueness
    violation — the duplicate-message test went through ON CONFLICT, so the
    exception never surfaced and the Conflict branch was never exercised."""
    session_id = f"s-{uuid.uuid4().hex[:8]}"
    await core.create_session(
        id=session_id, surface="telegram", surface_id=session_id,
        mind_id=a_mind, created_at=clock, last_active=clock)

    with pytest.raises(hive_data.Conflict):
        await core.create_session(
            id=session_id, surface="telegram", surface_id=session_id,
            mind_id=a_mind, created_at=clock, last_active=clock)


async def test_a_handled_failure_in_a_savepoint_leaves_the_unit_usable(
        connected, a_mind, clock, inspector):
    """The gateway's prevailing shape is try, log, carry on — legal against
    SQLite, and against Postgres it refuses every later statement in the
    transaction without reading it."""
    kept = f"s-{uuid.uuid4().hex[:8]}"
    async with hive_data.transaction() as unit:
        try:
            async with unit.savepoint():
                await core.add_message(
                    id=f"m-{uuid.uuid4().hex[:8]}",
                    conversation_id="no-such-conversation",
                    from_mind="skippy", to_mind="mordecai", message_number=1,
                    content="orphan", status="queued", timestamp=clock,
                    unit=unit)
        except hive_data.MissingReference:
            pass
        await core.create_session(
            id=kept, surface="telegram", surface_id=kept, mind_id=a_mind,
            created_at=clock, last_active=clock, unit=unit)

    assert await inspector.fetchval(
        'SELECT count(*) FROM core."sessions" WHERE "id" = $1', kept) == 1


async def test_a_caller_that_aborts_after_a_write_leaves_nothing_behind(
        connected, a_mind, clock, inspector):
    """The rollback the code actually branches on. A constraint violation
    cannot test it: Postgres has already aborted the transaction by then, so
    a COMMIT in place of the ROLLBACK degrades to a rollback anyway and the
    suite stays green."""
    session_id = f"s-{uuid.uuid4().hex[:8]}"

    with pytest.raises(RuntimeError):
        async with hive_data.transaction() as unit:
            await core.create_session(
                id=session_id, surface="telegram", surface_id=session_id,
                mind_id=a_mind, created_at=clock, last_active=clock,
                unit=unit)
            raise RuntimeError("the caller changed its mind")

    assert await inspector.fetchval(
        'SELECT count(*) FROM core."sessions" WHERE "id" = $1',
        session_id) == 0


async def test_a_unit_past_its_own_deadline_is_rolled_back(
        connected, a_mind, clock, inspector):
    """A unit holds a pooled connection, so one that never finishes is a
    connection no other operation can have."""
    session_id = f"s-{uuid.uuid4().hex[:8]}"

    with pytest.raises(hive_data.TimedOut):
        async with hive_data.transaction(timeout=0.5) as unit:
            await core.create_session(
                id=session_id, surface="telegram", surface_id=session_id,
                mind_id=a_mind, created_at=clock, last_active=clock,
                unit=unit)
            await asyncio.sleep(3)

    assert await inspector.fetchval(
        'SELECT count(*) FROM core."sessions" WHERE "id" = $1',
        session_id) == 0
    assert await core.get_mind(a_mind) is not None


async def test_a_unit_cannot_be_opened_from_a_helper_inside_a_generator(
        connected):
    """The obvious reaction to the guard firing is to move the write into a
    helper, which is the same hazard one frame further out."""
    async def write_something():
        async with hive_data.transaction():
            pass

    async def streaming_turn():
        await write_something()
        yield 1

    with pytest.raises(hive_data.TransactionMisuse):
        async for _ in streaming_turn():
            pass


async def test_a_unit_cannot_be_entered_through_an_exit_stack_in_a_generator(
        connected):
    from contextlib import AsyncExitStack

    async def streaming_turn():
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(hive_data.transaction())
            yield 1

    with pytest.raises(hive_data.TransactionMisuse):
        async for _ in streaming_turn():
            pass


async def test_a_hold_taken_after_a_read_is_refused(connected, a_mind):
    """A hold placed after the read does not protect that read: two units
    both read an unclaimed row, then both take the lock in turn, and both
    write. The method would be called correctly and still produce the
    outcome it exists to prevent."""
    with pytest.raises(hive_data.TransactionMisuse):
        async with hive_data.transaction() as unit:
            await core.get_mind(a_mind, unit=unit)
            await unit.hold("some-key")


# ---------------------------------------------------------------------------
# The conversation lock, which a unit cannot reach
# ---------------------------------------------------------------------------

async def test_a_conversation_lock_serializes_two_holders(connected):
    """The gateway's `send_message` is an async generator, so the lock it
    holds across its yields cannot live in a unit of work."""
    conversation = f"conv-{uuid.uuid4().hex[:8]}"
    order: list[str] = []

    async def work(label: str, delay: float) -> None:
        async with hive_data.conversation_lock(conversation):
            order.append(f"{label}-in")
            await asyncio.sleep(delay)
            order.append(f"{label}-out")

    await asyncio.gather(work("first", 0.2), work("second", 0.0))

    assert order in (
        ["first-in", "first-out", "second-in", "second-out"],
        ["second-in", "second-out", "first-in", "first-out"],
    ), order


async def test_a_non_blocking_conversation_lock_says_it_did_not_get_it(
        connected):
    """The gateway's turn lock is a test-and-refuse that answers "this
    conversation is mid-answer". Through a blocking lock that check becomes
    a silent wait ending in a timeout, which tells the user nothing."""
    conversation = f"conv-{uuid.uuid4().hex[:8]}"
    answers: list[bool] = []

    async def hold_it() -> None:
        async with hive_data.conversation_lock(conversation) as taken:
            answers.append(taken)
            await asyncio.sleep(0.4)

    held = asyncio.create_task(hold_it())
    await asyncio.sleep(0.1)
    async with hive_data.conversation_lock(conversation, wait=False) as taken:
        answers.append(taken)
    await held

    assert answers == [True, False]


async def test_a_conversation_lock_is_released_for_the_next_holder(connected):
    """A session-scope advisory lock outlives the statement, so a connection
    returned to the pool still holding one hands the next borrower a lock
    nobody asked for and nobody will release."""
    conversation = f"conv-{uuid.uuid4().hex[:8]}"
    async with hive_data.conversation_lock(conversation) as taken:
        assert taken is True

    async with hive_data.conversation_lock(conversation, wait=False) as again:
        assert again is True

    # Scoped to this run's own database. `pg_locks` is a server-wide view
    # and an advisory lock is namespaced per database, so an unscoped count
    # answers for every other suite run on the same server — which turns a
    # second concurrent run into a failure here and reads as a flaky test.
    held = await hive_data.run(lambda c: c.fetchval(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
        "AND database = (SELECT oid FROM pg_database "
        "WHERE datname = current_database())"))
    assert held == 0


# ---------------------------------------------------------------------------
# Arbitration that reported the wrong answer
# ---------------------------------------------------------------------------

async def test_expecting_a_column_to_be_null_can_actually_match(
        connected, a_mind, clock, inspector):
    """`= NULL` is never true, so the natural spelling of "only while this is
    still unset" matched nothing and reported zero rows — which the caller
    reads as having lost a race it won."""
    session_id = f"s-{uuid.uuid4().hex[:8]}"
    await core.create_session(
        id=session_id, surface="telegram", surface_id=session_id,
        mind_id=a_mind, created_at=clock, last_active=clock)

    won = await core.update_session_if(
        session_id, expected={"harness_sid": None}, harness_sid="thread-1")
    lost = await core.update_session_if(
        session_id, expected={"harness_sid": None}, harness_sid="thread-2")

    assert won["rows"] == 1
    assert lost["rows"] == 0
    assert await inspector.fetchval(
        'SELECT "harness_sid" FROM core."sessions" WHERE "id" = $1',
        session_id) == "thread-1"


async def test_a_conditional_update_with_an_empty_guard_is_refused(
        connected, a_mind, clock):
    """An empty guard is an unconditional update that reports it won a race
    it never entered — and the row count is the arbitration, so every
    contender would be told it was the winner."""
    session_id = f"s-{uuid.uuid4().hex[:8]}"
    await core.create_session(
        id=session_id, surface="telegram", surface_id=session_id,
        mind_id=a_mind, created_at=clock, last_active=clock)

    with pytest.raises(hive_data.UnsettableColumn):
        await core.update_session_if(
            session_id, expected={}, status="taken-over")


async def test_registering_a_mind_cannot_rewrite_another_minds_identity(
        connected, clock, inspector):
    """Conflicting on the name is how a second install scaffolded with the
    same MIND_NAME silently takes over an existing row and rewrites its
    UUID, orphaning every graph node and surface claim keyed to the old
    one."""
    name = f"name-{uuid.uuid4().hex[:8]}"
    first = f"mind-{uuid.uuid4().hex[:8]}"
    second = f"mind-{uuid.uuid4().hex[:8]}"
    await core.upsert_mind(
        mind_id=first, name=name, gateway_url="http://127.0.0.1:1",
        model="m", harness="claude", registered_at=clock, last_seen=clock)

    with pytest.raises(hive_data.Conflict):
        await core.upsert_mind(
            mind_id=second, name=name, gateway_url="http://127.0.0.1:2",
            model="m", harness="claude", registered_at=clock,
            last_seen=clock)

    assert await inspector.fetchval(
        'SELECT "mind_id" FROM core."minds" WHERE "name" = $1',
        name) == first


async def test_a_mind_that_was_renamed_still_registers(
        connected, clock, inspector):
    """The mirror case. Conflicting on the name made a renamed mind fail
    registration on every boot."""
    mind_id = f"mind-{uuid.uuid4().hex[:8]}"
    old = f"old-{uuid.uuid4().hex[:8]}"
    new = f"new-{uuid.uuid4().hex[:8]}"
    await core.upsert_mind(
        mind_id=mind_id, name=old, gateway_url="http://127.0.0.1:1",
        model="m", harness="claude", registered_at=clock, last_seen=clock)

    again = await core.upsert_mind(
        mind_id=mind_id, name=new, gateway_url="http://127.0.0.1:1",
        model="m", harness="claude", registered_at=clock, last_seen=clock)

    assert again["rows"] == 1
    assert await inspector.fetchval(
        'SELECT "name" FROM core."minds" WHERE "mind_id" = $1',
        mind_id) == new


async def test_an_unrelated_claim_write_does_not_reset_the_lockout(
        connected, a_mind, inspector):
    """`phrase_attempts` is the brute-force lockout on an unclaimed bot. A
    defaulted-and-therefore-always-written column put it back to zero on any
    claim write that was not about attempts, so the lockout never arrived."""
    await core.upsert_claim(mind_id=a_mind, surface="telegram")
    for _ in range(4):
        await core.count_phrase_attempt(mind_id=a_mind, surface="telegram")

    await core.upsert_claim(
        mind_id=a_mind, surface="telegram", bot_ref="@the_bot")

    assert await inspector.fetchval(
        'SELECT "phrase_attempts" FROM core."surface_claims" '
        'WHERE "mind_id" = $1 AND "surface" = $2', a_mind, "telegram") == 4


async def test_a_conditional_claim_write_refuses_an_empty_guard(
        connected, a_mind):
    await core.upsert_claim(mind_id=a_mind, surface="telegram")

    with pytest.raises(hive_data.UnsettableColumn):
        await core.claim_if(
            mind_id=a_mind, surface="telegram", expected={},
            claimed_ref="whoever")


async def test_a_conditional_claim_write_only_lands_while_unclaimed(
        connected, a_mind, clock, inspector):
    await core.upsert_claim(mind_id=a_mind, surface="discord")

    won = await core.claim_if(
        mind_id=a_mind, surface="discord", expected={"claimed_ref": None},
        claimed_ref="daniel", claimed_at=clock)
    lost = await core.claim_if(
        mind_id=a_mind, surface="discord", expected={"claimed_ref": None},
        claimed_ref="somebody-else", claimed_at=clock + 1)

    assert won["rows"] == 1
    assert lost["rows"] == 0
    assert await inspector.fetchval(
        'SELECT "claimed_ref" FROM core."surface_claims" '
        'WHERE "mind_id" = $1 AND "surface" = $2', a_mind,
        "discord") == "daniel"


# ---------------------------------------------------------------------------
# Columns nobody should be handed
# ---------------------------------------------------------------------------

async def test_a_mind_listing_does_not_carry_the_session_token(
        connected, a_mind, clock, inspector):
    """The edge design says the session token is stripped from every mind
    listing, because those listings answer to the service token every
    surface bot in the hive holds."""
    await hive_data.run(lambda c: c.execute(
        'UPDATE core."minds" SET "session_token" = $2 WHERE "mind_id" = $1',
        a_mind, "the-gateways-credential"))

    listed = await core.list_minds()
    one = await core.get_mind(a_mind)

    assert all("session_token" not in row for row in listed)
    assert "session_token" not in one
    # And it is still reachable for the one caller that needs it.
    assert await core.mind_session_token(a_mind) == "the-gateways-credential"


async def test_a_credential_row_does_not_carry_its_digest(connected, clock):
    holder = f"holder-{uuid.uuid4().hex[:8]}"
    digest = f"sha-{uuid.uuid4().hex}"
    await core.upsert_credential(
        holder=holder, username="scheduler", role="service",
        token_sha=digest, created_at=clock)

    assert "token_sha" not in (await core.get_credential(holder))

    found = await core.credential_for_token(digest)
    assert found["holder"] == holder
    assert "token_sha" not in found


async def test_a_revoked_credential_does_not_authenticate(connected, clock):
    """A credential that authenticates after it was revoked is the whole
    failure the column exists to prevent."""
    holder = f"holder-{uuid.uuid4().hex[:8]}"
    digest = f"sha-{uuid.uuid4().hex}"
    await core.upsert_credential(
        holder=holder, username="scheduler", role="service",
        token_sha=digest, created_at=clock)
    assert await core.credential_for_token(digest) is not None

    await core.revoke_credential(holder, clock + 1)

    assert await core.credential_for_token(digest) is None


async def test_a_user_row_does_not_carry_its_password_hash(connected, clock):
    username = f"user-{uuid.uuid4().hex[:8]}"
    await core.create_user(
        username=username, password_hash="argon2:secret",  # secret-guard: allow — an invented hash in a test fixture
        created_at=int(clock))

    assert "password_hash" not in (await core.get_user(username))


# ---------------------------------------------------------------------------
# Reads and writes the gateway needs and the first pass could not express
# ---------------------------------------------------------------------------

async def test_a_boot_reconciliation_moves_every_running_session(
        connected, a_mind, clock, inspector):
    """A gateway that died with conversations running finds them still
    marked running and nothing answering for them. A write keyed to one id
    cannot express it."""
    marker = uuid.uuid4().hex[:8]
    for n in range(3):
        await core.create_session(
            id=f"s-{marker}-{n}", surface="telegram",
            surface_id=f"chat-{marker}-{n}", mind_id=a_mind,
            created_at=clock, last_active=clock, status="running")

    moved = await core.reconcile_session_status(was="running", now="idle")

    assert moved["rows"] >= 3
    assert await inspector.fetchval(
        'SELECT count(*) FROM core."sessions" WHERE "status" = $1',
        "running") == 0


async def test_a_turn_can_be_deleted_by_the_identity_it_was_given(
        connected, a_mind, clock, inspector):
    """An overflowed turn has to come back out of the ledger, which is why
    the insert returns its identity at all."""
    session_id = f"s-{uuid.uuid4().hex[:8]}"
    await core.create_session(
        id=session_id, surface="telegram", surface_id=session_id,
        mind_id=a_mind, created_at=clock, last_active=clock)
    kept = await core.record_turn(
        session_id=session_id, role="user", content="keep", created_at=clock)
    dropped = await core.record_turn(
        session_id=session_id, role="user", content="drop",
        created_at=clock + 1)

    assert (await core.delete_turn(dropped["row"]["id"]))["rows"] == 1

    remaining = await inspector.fetch(
        'SELECT "id" FROM core."session_turns" WHERE "session_id" = $1',
        session_id)
    assert [r["id"] for r in remaining] == [kept["row"]["id"]]


async def test_the_surfaces_holding_a_session_can_be_found_and_released(
        connected, a_mind, clock, inspector):
    session_id = f"s-{uuid.uuid4().hex[:8]}"
    await core.create_session(
        id=session_id, surface="telegram", surface_id=session_id,
        mind_id=a_mind, created_at=clock, last_active=clock)
    for client_type in ("telegram", "terminal"):
        await core.bind_surface(
            client_type=client_type, client_ref=session_id,
            session_id=session_id)

    found = await core.bindings_for_session(session_id)
    assert sorted(r["client_type"] for r in found) == ["telegram", "terminal"]

    assert (await core.unbind_session(session_id))["rows"] == 2
    assert await inspector.fetchval(
        'SELECT count(*) FROM core."active_sessions" '
        'WHERE "session_id" = $1', session_id) == 0


async def test_a_mind_resolves_by_identity_or_by_name_whatever_the_case(
        connected, clock):
    mind_id = f"mind-{uuid.uuid4().hex[:8]}"
    name = f"mixedcase-{uuid.uuid4().hex[:6]}"
    await core.upsert_mind(
        mind_id=mind_id, name=name, gateway_url="http://127.0.0.1:1",
        model="m", harness="claude", registered_at=clock, last_seen=clock)

    assert (await core.get_mind_by_id_or_name(mind_id))["name"] == name
    assert (await core.get_mind_by_id_or_name(
        name.upper()))["mind_id"] == mind_id


async def test_a_message_number_comes_from_the_conversation_it_joins(
        connected, clock):
    conversation = f"conv-{uuid.uuid4().hex[:8]}"
    await core.create_conversation(id=conversation, created_at=clock)

    assert await core.next_message_number(conversation) == 1
    async with hive_data.transaction() as unit:
        number = await core.next_message_number(conversation, unit=unit)
        await core.add_message(
            id=f"m-{uuid.uuid4().hex[:8]}", conversation_id=conversation,
            from_mind="skippy", to_mind="mordecai", message_number=number,
            content="first", status="queued", timestamp=clock, unit=unit)

    assert await core.next_message_number(conversation) == 2


async def test_a_filter_that_matches_nothing_returns_nothing(
        connected, a_mind, clock):
    """An empty list is a filter matching nothing, not an absent filter.
    Returning every session instead is the widest possible answer to the
    narrowest possible question."""
    await core.create_session(
        id=f"s-{uuid.uuid4().hex[:8]}", surface="telegram",
        surface_id="chat-x", mind_id=a_mind, created_at=clock,
        last_active=clock)

    assert await core.list_sessions(surfaces=[]) == []
    assert await core.list_sessions(mind_id=a_mind) != []


async def test_a_queued_resend_is_found_with_the_columns_it_needs(
        connected, a_mind, clock):
    """Needs the resend columns, which every ordinary read withholds."""
    session_id = f"s-{uuid.uuid4().hex[:8]}"
    await core.create_session(
        id=session_id, surface="telegram", surface_id=session_id,
        mind_id=a_mind, created_at=clock, last_active=clock)
    await core.update_session(session_id, resend_content="say it again")

    waiting = await core.pending_resends(statuses_excluded=["closed"])

    mine = [r for r in waiting if r["id"] == session_id]
    assert len(mine) == 1
    assert mine[0]["resend_content"] == "say it again"


async def test_the_lowest_overhead_for_a_harness_is_readable(
        connected, a_mind, clock):
    for model, tokens in (("a", 5000), ("b", 3000), ("c", 7000)):
        await core.record_overhead(
            mind_id=a_mind, harness="claude", model=model, surface="telegram",
            tokens=tokens, observed_at=clock)

    assert await core.least_overhead(
        mind_id=a_mind, harness="claude") == 3000
    assert await core.least_overhead(mind_id=a_mind, harness="codex") is None


async def test_turns_can_be_counted_by_role_and_since_a_moment(
        connected, a_mind, clock):
    session_id = f"s-{uuid.uuid4().hex[:8]}"
    await core.create_session(
        id=session_id, surface="telegram", surface_id=session_id,
        mind_id=a_mind, created_at=clock, last_active=clock)
    for role, at in (("user", clock), ("assistant", clock),
                     ("user", clock + 100)):
        await core.record_turn(
            session_id=session_id, role=role, content="x", created_at=at)

    assert await core.count_turns(session_id) == 3
    assert await core.count_turns(session_id, roles=["user"]) == 2
    assert await core.count_turns(
        session_id, roles=["user"], since=clock + 50) == 1


# ---------------------------------------------------------------------------
# Startup refusals
# ---------------------------------------------------------------------------

async def test_a_sequence_behind_its_rows_refuses_at_startup(scratch_dsn):
    """What a bulk load leaves. The next insert collides as a uniqueness
    conflict — the one outcome a caller is told is benign — so a ledger
    would record nothing while reporting a harmless duplicate."""
    scratch = await open_claimed(ADMIN_DSN, "hive_data_seq_")
    other_dsn = scratch.dsn
    setup = await asyncpg.connect(other_dsn)
    try:
        await setup.execute(
            (Path(hive_data.__file__).parent / "schema" / "core.sql"
             ).read_text())
        await setup.execute(
            "INSERT INTO core.minds (mind_id,name,gateway_url,model,harness,"
            "registered_at,last_seen) VALUES ('m','n','u','mo','claude',1,1)")
        await setup.execute(
            "INSERT INTO core.sessions (id,surface,surface_id,mind_id,"
            "created_at,last_active,status) "
            "VALUES ('s','telegram','c','m',1,1,'running')")
        for identity in (1, 2, 3):
            await setup.execute(
                "INSERT INTO core.session_turns (id,session_id,role,content,"
                "created_at) VALUES ($1,'s','user','x',1)", identity)
    finally:
        await setup.close()

    try:
        with pytest.raises(hive_data.SequenceBehind) as caught:
            await hive_data.connect(hive_data.Settings(dsn=other_dsn))
        assert "core.session_turns.id" in str(caught.value)
    finally:
        await hive_data.close(drain_timeout=5.0)
        await scratch.close()


def test_a_command_status_that_cannot_be_read_is_not_reported_as_zero():
    """The count decides which caller won a race, so a zero nobody measured
    is a caller told it lost for no reason."""
    from hive_data.rows import row_count

    assert row_count("UPDATE 0") == 0
    assert row_count("INSERT 0 1") == 1
    with pytest.raises(hive_data.HiveDataError):
        row_count("CREATE TABLE")


async def test_closing_the_pool_does_not_wait_for_ever_on_wedged_work(
        scratch_dsn):
    """A shutdown that hangs on one wedged connection never reaches the rest
    of the shutdown, and the process is killed with its other work
    half-done."""
    await hive_data.connect(hive_data.Settings(
        dsn=scratch_dsn, pool_min=1, pool_max=2, statement_timeout=30.0))

    async def wedged() -> None:
        await hive_data.run(lambda c: c.execute("SELECT pg_sleep(20)"))

    busy = asyncio.create_task(wedged())
    await asyncio.sleep(0.3)

    started = asyncio.get_running_loop().time()
    await hive_data.close(drain_timeout=1.0)
    took = asyncio.get_running_loop().time() - started

    assert took < 10, f"close waited {took:.1f}s on a wedged connection"
    busy.cancel()
    await asyncio.gather(busy, return_exceptions=True)
    with pytest.raises(hive_data.NotInitialised):
        hive_data.live_pool()


async def test_the_statement_deadline_is_set_on_the_server_too(scratch_dsn):
    """The client holding a statement open may be the process that has
    stopped running, which is the one case a client-side deadline cannot
    cover."""
    await hive_data.connect(hive_data.Settings(
        dsn=scratch_dsn, statement_timeout=7.0))
    try:
        applied = await hive_data.run(lambda c: c.fetchval(
            "SELECT current_setting('statement_timeout')"))
        idle = await hive_data.run(lambda c: c.fetchval(
            "SELECT current_setting('idle_in_transaction_session_timeout')"))
    finally:
        await hive_data.close(drain_timeout=5.0)

    assert applied == "7s"
    assert idle not in ("0", "")


async def test_a_pool_opened_on_a_dead_loop_is_refused_not_reused(
        scratch_dsn):
    """asyncpg ties a pool to the loop that created it, so a reused one
    fails on its first statement with a closed-loop error far from the call
    that reused it."""
    import threading

    outcome: dict[str, object] = {}

    def first_loop() -> None:
        async def go() -> None:
            await hive_data.connect(hive_data.Settings(dsn=scratch_dsn))
        asyncio.run(go())

    thread = threading.Thread(target=first_loop)
    thread.start()
    thread.join()

    try:
        with pytest.raises(hive_data.HiveDataError) as caught:
            await hive_data.open_pool(hive_data.Settings(dsn=scratch_dsn))
        outcome["message"] = str(caught.value)
    finally:
        hive_data.pool._pool = None
        hive_data.pool._settings = None
        hive_data.pool._loop = None

    assert "another event loop" in str(outcome["message"])
