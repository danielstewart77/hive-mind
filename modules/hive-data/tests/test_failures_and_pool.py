"""The failures, told apart, and the pool's own lifecycle.

A refusal reported as the wrong refusal sends somebody to the wrong machine.
Three of these outcomes have different remedies and one of them is "nothing
is wrong with the database at all", so none of them may arrive as another.
"""

from __future__ import annotations

import asyncio
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest

import hive_data

from tests._scratch import open_claimed
from tests.conftest import ADMIN_DSN
from hive_data import core


def _swap(dsn: str, **parts: str) -> str:
    split = urlsplit(dsn)
    userinfo, _, hostport = split.netloc.rpartition("@")
    user, _, password = userinfo.partition(":")
    user = parts.get("user", user)
    password = parts.get("password", password)
    host, _, port = hostport.partition(":")
    host = parts.get("host", host)
    port = parts.get("port", port)
    database = parts.get("database", split.path.lstrip("/"))
    return urlunsplit(split._replace(
        netloc=f"{user}:{password}@{host}:{port}", path=f"/{database}"))


async def test_a_port_nobody_is_listening_on_reports_unreachable(scratch_dsn):
    """Not folded in with a refused credential: those are two different
    machines to go and look at."""
    settings = hive_data.Settings(
        dsn=_swap(scratch_dsn, host="127.0.0.1", port="5399"),
        connect_timeout=5.0)

    with pytest.raises(hive_data.Unreachable):
        await hive_data.connect(settings)


async def test_a_wrong_password_reports_a_refused_credential(scratch_dsn):
    """Produced over the container network on purpose. `pg_hba.conf` trusts
    loopback and demands scram from everywhere else, so a wrong password over
    loopback succeeds and this test would pass for the wrong reason."""
    settings = hive_data.Settings(
        # An invented string. The whole point of the test is that the
        # server refuses it.
        dsn=_swap(scratch_dsn, password="definitely-not-the-password"),  # secret-guard: allow
        connect_timeout=5.0)

    with pytest.raises(hive_data.CredentialRefused):
        await hive_data.connect(settings)


async def test_a_database_that_is_not_there_is_its_own_outcome(scratch_dsn):
    """The server answered. Reporting this as unreachable sends the operator
    to a machine that is up and serving everybody else."""
    settings = hive_data.Settings(
        dsn=_swap(scratch_dsn, database=f"absent_{uuid.uuid4().hex[:8]}"),
        connect_timeout=5.0)

    with pytest.raises(hive_data.DatabaseMissing):
        await hive_data.connect(settings)


async def test_a_statement_past_its_deadline_reports_a_timeout(scratch_dsn):
    """Without a deadline a wedged statement presents as a mind still
    thinking, which is the one failure mode nothing on screen can explain.
    The live server has `statement_timeout` set to zero."""
    settings = hive_data.Settings(dsn=scratch_dsn, statement_timeout=0.5)
    await hive_data.connect(settings)
    try:
        with pytest.raises(hive_data.TimedOut):
            await hive_data.run(lambda c: c.execute("SELECT pg_sleep(5)"))
    finally:
        await hive_data.close(drain_timeout=5.0)


async def test_an_exhausted_pool_is_not_reported_as_unreachable(scratch_dsn):
    """The database is answering; this process has run out of connections to
    ask it with, and the remedy is the pool's size rather than the server."""
    settings = hive_data.Settings(
        dsn=scratch_dsn, pool_min=1, pool_max=1, acquire_timeout=0.5)
    await hive_data.connect(settings)
    try:
        async def hog() -> None:
            await hive_data.run(lambda c: c.execute("SELECT pg_sleep(3)"))

        busy = asyncio.create_task(hog())
        await asyncio.sleep(0.2)
        with pytest.raises(hive_data.NoConnectionAvailable):
            await core.list_minds()
        busy.cancel()
        await asyncio.gather(busy, return_exceptions=True)
    finally:
        await hive_data.close(drain_timeout=5.0)


async def test_an_operation_before_anything_opened_a_pool_says_so(
        scratch_dsn, monkeypatch):
    """Nothing opens one on demand: a lazy open under two concurrent first
    calls builds two pools and holds double the connections with nothing
    reporting it."""
    monkeypatch.setenv(hive_data.config.DSN_VAR, scratch_dsn)

    with pytest.raises(hive_data.NotInitialised):
        await core.list_minds()


async def test_opening_twice_hands_back_one_pool_even_under_concurrency(
        scratch_dsn, monkeypatch):
    monkeypatch.setenv(hive_data.config.DSN_VAR, scratch_dsn)
    try:
        pools = await asyncio.gather(*[
            hive_data.open_pool() for _ in range(5)])

        assert len({id(p) for p in pools}) == 1
    finally:
        await hive_data.close(drain_timeout=5.0)


async def test_an_unnamed_database_refuses_rather_than_guessing(monkeypatch):
    monkeypatch.delenv(hive_data.config.DSN_VAR, raising=False)

    with pytest.raises(hive_data.NotConfigured):
        hive_data.from_environment()


async def test_a_deadline_of_zero_is_refused_as_configuration(monkeypatch):
    """Unsetting it takes the default. Setting it to zero would be asking
    for the behaviour every one of these deadlines exists to end."""
    monkeypatch.setenv(hive_data.config.DSN_VAR, "postgresql://h/x")
    monkeypatch.setenv(hive_data.config.STATEMENT_TIMEOUT_VAR, "0")

    with pytest.raises(hive_data.NotConfigured):
        hive_data.from_environment()


async def test_the_configuration_names_the_database_without_its_credential(
        monkeypatch):
    """Reported when the pool opens, because a service pointed at a
    half-loaded database answers plausibly and 8,000 rows short."""
    monkeypatch.setenv(
        hive_data.config.DSN_VAR,
        "postgresql://hive:placeholder-not-a-credential@192.168.48.33:5432/hive")

    described = hive_data.from_environment().describes

    assert described == "192.168.48.33:5432/hive"
    assert "placeholder-not-a-credential" not in described


async def test_pointing_the_configuration_at_another_database_reads_that_one(
        scratch_dsn, clock):
    """The load-bearing half of "the address is configuration": the pool is
    keyed to the configured database, so a reconfigured package cannot keep
    answering from the first one."""
    import asyncpg

    scratch = await open_claimed(ADMIN_DSN, "hive_data_other_")
    other_dsn = scratch.dsn
    setup = await asyncpg.connect(other_dsn)
    try:
        from pathlib import Path
        await setup.execute(
            (Path(hive_data.__file__).parent / "schema" / "core.sql").read_text())
    finally:
        await setup.close()

    try:
        await hive_data.connect(hive_data.Settings(dsn=scratch_dsn))
        await core.upsert_mind(
            mind_id="only-in-the-first", name="first-only",
            gateway_url="http://127.0.0.1:1", model="m", harness="claude",
            registered_at=clock, last_seen=clock)
        await hive_data.close(drain_timeout=5.0)

        await hive_data.connect(hive_data.Settings(dsn=other_dsn))
        assert await core.get_mind("only-in-the-first") is None
        assert await core.list_minds() == []
    finally:
        await hive_data.close(drain_timeout=5.0)
        await scratch.close()


async def test_a_second_pool_against_another_database_is_refused(scratch_dsn):
    """Two pools in one process would let two operations in one request
    answer from two databases."""
    await hive_data.connect(hive_data.Settings(dsn=scratch_dsn))
    try:
        with pytest.raises(hive_data.HiveDataError) as caught:
            await hive_data.open_pool(
                hive_data.Settings(dsn=_swap(scratch_dsn, database="other")))
        assert "close it before opening one" in str(caught.value)
    finally:
        await hive_data.close(drain_timeout=5.0)


async def test_a_hundred_operations_at_once_run_on_more_than_one_connection(
        connected, a_mind, clock):
    """A single connection behind an `asyncio.Lock` would pass "they all
    completed" identically to a pool, so the concurrency is asserted on the
    server's own view of its backends."""
    session_ids = [f"s-{uuid.uuid4().hex[:10]}" for _ in range(100)]
    await asyncio.gather(*[
        core.create_session(
            id=sid, surface="telegram", surface_id=sid, mind_id=a_mind,
            created_at=clock, last_active=clock, opening_line=sid)
        for sid in session_ids
    ])

    found = await asyncio.gather(*[
        core.get_session(sid) for sid in session_ids])

    # Each coroutine got its own row, not whichever row was in flight.
    assert [row["opening_line"] for row in found] == session_ids

    backends = await hive_data.run(lambda c: c.fetchval(
        "SELECT count(DISTINCT pid) FROM pg_stat_activity "
        "WHERE application_name = 'hive-data' AND datname = current_database()"
    ))
    assert backends > 1


async def test_a_schema_missing_a_declared_column_refuses_at_startup(
        scratch_dsn, monkeypatch):
    """A column this package names and the database does not have otherwise
    arrives as a missing dict key, and the first consumer reads one of those
    as a value: `session.get("rotation_armed") or 0` is "nothing armed" on
    the one column whose three states decide whether a permanent session row
    is kept or retired."""
    monkeypatch.setitem(
        hive_data.COLUMNS, "sessions",
        hive_data.COLUMNS["sessions"] + ("a_column_nobody_applied",))

    with pytest.raises(hive_data.SchemaMismatch) as caught:
        await hive_data.connect(hive_data.Settings(dsn=scratch_dsn))

    assert "core.sessions.a_column_nobody_applied" in str(caught.value)
    # And it refused without leaving a pool open behind it.
    with pytest.raises(hive_data.NotInitialised):
        hive_data.live_pool()
