"""Do the two halves of the console's store answer the same thing?

`store.py` has two backends and the whole claim made for keeping both is that
unsetting one environment variable returns the console to the behaviour it
had. Nothing else in this suite tests that: `conftest._isolated_hive_db`
pins every other test to the SQLite half on purpose, so without this module
the entire Postgres branch of `store.py` could be deleted and the suite would
stay green.

`hive_data`'s own suite guards each operation against a real Postgres. What
no suite guards is the *wiring*: which `hive_data` function each store
operation calls, with which argument order, and which key it reads off the
result (`["rows"]`, `["row"]`). That rested on reading `store.py`.

So every test here runs one sequence twice — once against a temporary SQLite
file, once against a scratch Postgres database this module creates and drops
— and compares what came back. A difference is the finding; the value itself
is secondary, and each test asserts on it too so that two halves agreeing on
a wrong answer is still a failure.

Skipped, cleanly, when no Postgres is named. `HIVE_DATA_TEST_ADMIN_DSN` says
where one is, with no default, because a default would be a credential in a
tracked file.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

import kanban_data
import store

ADMIN_DSN = os.environ.get("HIVE_DATA_TEST_ADMIN_DSN", "").strip()

NO_SERVER = (
    "HIVE_DATA_TEST_ADMIN_DSN is unset. It names a Postgres this suite may "
    "create and drop a database on. Without it the two halves cannot be "
    "compared and these tests are skipped rather than passing on one half."
)

#: The schemas to apply to the scratch database, as applied, vendored in the
#: access layer. Read from the installed package rather than copied here, so
#: a schema change arrives instead of being silently out of date.
#:
#: `core.sql` as well as `board.sql`, and in that order: `board` carries real
#: foreign keys into `core.sessions` and `core.minds`, which is the first
#: thing in this database that could not exist while each store was its own
#: SQLite file. `memory.sql` too, for the browser — it needs the `vector`
#: extension, which is not a trusted one, so the scratch database is created
#: by the same credential `hive_data`'s own suite uses.
_SCHEMA_ORDER = ("core.sql", "memory.sql", "board.sql")


def _schemas() -> list[str]:
    import hive_data

    directory = Path(hive_data.__file__).parent / "schema"
    return [(directory / name).read_text() for name in _SCHEMA_ORDER]


async def _on_sqlite(sequence: Callable[[], Awaitable[Any]],
                     db_path: Path) -> Any:
    """Run a sequence against a temporary SQLite file."""
    os.environ[store.DSN_VAR] = ""
    await store.open(db_path)
    try:
        assert not store.on_postgres()
        return await sequence()
    finally:
        await store.close()


async def _on_postgres(sequence: Callable[[], Awaitable[Any]]) -> Any:
    """Run the same sequence against a scratch Postgres database."""
    import asyncpg

    name = f"hive_data_parity_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    base, _, _ = ADMIN_DSN.rpartition("/")
    dsn = f"{base}/{name}"
    try:
        target = await asyncpg.connect(dsn)
        try:
            for schema in _schemas():
                await target.execute(schema)
        finally:
            await target.close()
        os.environ[store.DSN_VAR] = dsn
        await store.open()
        try:
            assert store.on_postgres()
            return await sequence()
        finally:
            await store.close()
    finally:
        os.environ[store.DSN_VAR] = ""
        admin = await asyncpg.connect(ADMIN_DSN)
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()", name)
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await admin.close()


def both_halves(sequence: Callable[[], Awaitable[Any]],
                db_path: Path) -> tuple[Any, Any]:
    """What each half answered, for the same sequence of operations.

    Run in separate event loops, one after the other, because each half opens
    and closes its own store and the Postgres pool belongs to the loop it was
    opened on.
    """
    if not ADMIN_DSN:
        pytest.skip(NO_SERVER)
    on_file = asyncio.run(_on_sqlite(sequence, db_path))
    on_server = asyncio.run(_on_postgres(sequence))
    return on_file, on_server


# ---------------------------------------------------------------------------
# the board


def test_the_two_halves_build_and_number_the_same_board(tmp_path: Path) -> None:
    """The numbering is the risk the whole cutover carries: every story's
    number appears in conversations, commit messages and the lucent graph, so
    two halves numbering differently is a port that renames work."""
    async def sequence() -> list[Any]:
        epic = await kanban_data.create_epic("An epic", description="why")
        feature = await kanban_data.create_feature(epic["id"], "A feature")
        first = await kanban_data.create_story("First story",
                                               feature_id=feature["id"])
        second = await kanban_data.create_story("Second story",
                                                feature_id=feature["id"])
        loose = await kanban_data.create_story("Under the epic",
                                               epic_id=epic["id"])
        numbers = await kanban_data.backlog_numbers()
        return [
            [row["title"] for row in (await kanban_data.get_data())["epics"]],
            [row["title"]
             for row in (await kanban_data.get_data())["features"]],
            [row["title"]
             for row in (await kanban_data.get_data())["stories"]],
            [numbers[item["id"]]
             for item in (epic, feature, first, second, loose)],
            (await kanban_data.find_item(second["id"]))["backlog_number"],
        ]

    on_file, on_server = both_halves(sequence, tmp_path / "parity.db")
    assert on_file == on_server
    epics, features, stories, numbers, one_number = on_file
    assert epics == ["An epic"]
    assert features == ["A feature"]
    assert stories == ["First story", "Second story", "Under the epic"]
    assert numbers == ["1", "1.1", "1.1.1", "1.1.2", "1.2"]
    assert one_number == "1.1.2"


def test_the_two_halves_reorder_a_sibling_list_the_same_way(
        tmp_path: Path) -> None:
    """A drop renumbers the whole sibling list. Two halves writing a
    different order is a board that looks different in two browsers."""
    async def sequence() -> list[Any]:
        epic = await kanban_data.create_epic("Epic")
        feature = await kanban_data.create_feature(epic["id"], "Feature")
        stories = [
            await kanban_data.create_story(f"Story {index}",
                                           feature_id=feature["id"])
            for index in range(3)
        ]
        await kanban_data.place_before(stories[2]["id"], stories[0]["id"])
        after = [row["title"] for row in await kanban_data.list_stories()]
        await kanban_data.update_story(stories[1]["id"],
                                       before_id=stories[0]["id"])
        return [after,
                [row["title"] for row in await kanban_data.list_stories()]]

    on_file, on_server = both_halves(sequence, tmp_path / "parity.db")
    assert on_file == on_server
    first, second = on_file
    assert first == ["Story 2", "Story 0", "Story 1"]
    assert second == ["Story 2", "Story 1", "Story 0"]


def test_the_two_halves_delete_a_subtree_and_count_it_the_same(
        tmp_path: Path) -> None:
    """What went, not what the page predicted. The count is rendered, and
    the cascade is the database's on one half and a recursive statement on
    the other."""
    async def sequence() -> list[Any]:
        epic = await kanban_data.create_epic("Doomed")
        survivor = await kanban_data.create_epic("Survivor")
        feature = await kanban_data.create_feature(epic["id"], "Feature")
        await kanban_data.create_story("Nested", feature_id=feature["id"])
        await kanban_data.create_story("Direct", epic_id=epic["id"])
        await kanban_data.create_story("Elsewhere", epic_id=survivor["id"])
        predicted = await kanban_data.count_descendants(epic["id"], "epic")
        removed = (await kanban_data.delete_epic(epic["id"]))["removed"]
        return [predicted, removed,
                sorted(row["title"]
                       for row in await kanban_data.list_stories())]

    on_file, on_server = both_halves(sequence, tmp_path / "parity.db")
    assert on_file == on_server
    predicted, removed, left = on_file
    assert predicted == {"features": 1, "stories": 2}
    assert removed == {"features": 1, "stories": 2}
    assert left == ["Elsewhere"]


def test_the_two_halves_retype_an_item_the_same_way(tmp_path: Path) -> None:
    """A retype is five columns at once, and the console reads the board
    between any two writes: a feature holding a story's status, or a row at a
    duplicate number, is what writing them one at a time looks like."""
    async def sequence() -> list[Any]:
        epic = await kanban_data.create_epic("Epic")
        feature = await kanban_data.create_feature(epic["id"], "Feature")
        story = await kanban_data.create_story("Story",
                                               feature_id=feature["id"])
        as_bug = await kanban_data.change_type(story["id"], "bug")
        kind_now = await kanban_data.current_type(story["id"])
        as_epic = await kanban_data.change_type(feature["id"], "epic")
        # The ids are minted per run, so what is compared is the shape: the
        # keys a record carries are what the board page selects on.
        return [as_bug.get("subtype"), as_bug.get("status"), kind_now,
                sorted(as_epic), await kanban_data.kind_of(feature["id"])]

    on_file, on_server = both_halves(sequence, tmp_path / "parity.db")
    assert on_file == on_server
    subtype, status, kind_now, epic_keys, kind = on_file
    assert subtype == "bug"
    assert status == "backlog"
    assert kind_now == "bug"
    # An epic's record carries no parent key at all, whatever `parent_id`
    # holds — the board selects a feature by `epic_id` and would draw one.
    assert "epic_id" not in epic_keys and "parent_id" not in epic_keys
    assert kind == "epic"


async def _conversations(*ids: str) -> None:
    """Session rows for the conversations a design session will point at.

    A no-op on the SQLite half, which has no `core` tables and no key to
    satisfy: the console's own file never held the gateway's sessions.
    """
    if not store.on_postgres():
        return
    from hive_data import core

    mind_id = "parity-mind"
    await core.upsert_mind(mind_id=mind_id, name="parity",
                           gateway_url="http://parity:8421",
                           model="a-model", harness="claude",
                           registered_at=1.0, last_seen=1.0, owner="daniel")
    for conversation_id in ids:
        await core.create_session(
            id=conversation_id, conversation_id=conversation_id,
            surface="console", surface_id="tile", model="a-model",
            created_at=1.0, last_active=1.0, mind_id=mind_id)


# ---------------------------------------------------------------------------
# design sessions and their notices


def test_the_two_halves_guard_a_page_write_on_its_revision_the_same_way(
        tmp_path: Path) -> None:
    """The revision is in the statement, so the row count *is* the
    arbitration: two halves disagreeing about it is one browser tab's work
    silently replaced by another's with both told they succeeded."""
    async def sequence() -> list[Any]:
        await store.create_design_session("d-1", "A design", 1.0, 1.0)
        first = await store.write_design_page(
            "d-1", "requirements", '{"r1": {}}', "{}", "{}", 1, 2.0)
        stale = await store.write_design_page(
            "d-1", "requirements", '{"r1": {"text": "clobbered"}}', "{}",
            "{}", 1, 3.0)
        row = await store.design_session("d-1")
        return [first, stale, row["revision"], row["requirements"]]

    on_file, on_server = both_halves(sequence, tmp_path / "parity.db")
    assert on_file == on_server
    first, stale, revision, requirements = on_file
    assert [first, stale] == [1, 0]
    assert revision == 2
    assert requirements == '{"r1": {}}'


def test_the_two_halves_claim_a_notice_once(tmp_path: Path) -> None:
    """Build is the one press that starts a harness writing code. Delivered
    twice is two processes editing one working tree, so the loser has to be
    told it lost rather than handed a second copy."""
    async def sequence() -> list[Any]:
        await store.create_design_session("d-1", "A design", 1.0, 1.0)
        notice_id = await store.queue_notice("d-1", "build", "functions", 2.0,
                                             body="go")
        won = await store.claim_notice(notice_id, 3.0)
        lost = await store.claim_notice(notice_id, 4.0)
        released = await store.release_notice(notice_id)
        again = await store.claim_notice(notice_id, 5.0)
        delivered = await store.mark_notice_delivered(notice_id, 6.0)
        twice = await store.mark_notice_delivered(notice_id, 7.0)
        return [
            won is not None and won["body"], lost, released,
            again is not None, delivered, twice,
            [row["kind"] for row in await store.pending_notices("d-1")],
        ]

    on_file, on_server = both_halves(sequence, tmp_path / "parity.db")
    assert on_file == on_server
    body, lost, released, again, delivered, twice, pending = on_file
    assert body == "go"
    assert lost is None
    assert [released, again, delivered, twice] == [1, True, 1, 0]
    assert pending == []


def test_the_two_halves_keep_what_an_attach_was_not_told_about(
        tmp_path: Path) -> None:
    """A mind re-claims a session on every turn that touches it, knowing the
    conversation and nothing else. An attach that wrote null instead would
    blank the mind the page was published by, every turn."""
    async def sequence() -> list[Any]:
        # The conversations exist first. `design_sessions.conversation_id` is
        # a real foreign key into `core.sessions` on the one database, which
        # it could never be while each store was its own SQLite file — so a
        # sequence attaching to an id nothing minted is refused there and
        # accepted here, and would read as a difference in this operation.
        await _conversations("conv-1", "conv-2")
        await store.create_design_session("d-1", "A design", 1.0, 1.0,
                                          surface="console")
        await store.attach_design_session("d-1", "conv-1", 2.0)
        kept = await store.design_session("d-1")
        await store.attach_design_session("d-1", "conv-2", 3.0,
                                          surface="telegram")
        moved = await store.design_session("d-1")
        return [kept["conversation_id"], kept["surface"],
                moved["conversation_id"], moved["surface"]]

    on_file, on_server = both_halves(sequence, tmp_path / "parity.db")
    assert on_file == on_server
    assert on_file == ["conv-1", "console", "conv-2", "telegram"]


def test_the_two_halves_take_a_session_s_notices_with_it(
        tmp_path: Path) -> None:
    """`design_notices.design_id` carries no foreign key, so nothing in the
    database removes a session's notices when the session goes — and left
    behind they are undelivered rows the delivery path keeps picking up and
    cannot join to anything."""
    async def sequence() -> list[Any]:
        await store.create_design_session("d-1", "A design", 1.0, 1.0)
        await store.queue_notice("d-1", "feedback", "requirements", 2.0)
        removed = await store.delete_design_session("d-1")
        absent = await store.delete_design_session("d-1")
        return [removed, absent, await store.pending_notices(),
                await store.design_session("d-1")]

    on_file, on_server = both_halves(sequence, tmp_path / "parity.db")
    assert on_file == on_server
    assert on_file == [1, 0, [], None]


# ---------------------------------------------------------------------------
# settings, operations and people


def test_the_two_halves_replace_a_capability_endpoint_the_same_way(
        tmp_path: Path) -> None:
    """An upsert on both. A write that inserted twice would leave two rows
    for one node, and a read would answer whichever the planner reached."""
    async def sequence() -> list[Any]:
        await store.set_capability_endpoint("ollama", "http://one:11434", 1)
        await store.set_capability_endpoint("ollama", "http://two:11434", 2)
        row = await store.capability_endpoint("ollama")
        cleared = await store.clear_capability_endpoint("ollama")
        absent = await store.clear_capability_endpoint("ollama")
        await store.set_edge_mind("skippy", "operator", "operator",
                                  "systemd", 3)
        await store.set_edge_mind("skippy", "standard", "sandboxed",
                                  "container", 4)
        return [row["endpoint"], row["updated_at"], cleared, absent,
                await store.capability_endpoint("ollama"),
                [(m["name"], m["profile"], m["role"], m["deployment"])
                 for m in await store.edge_minds()]]

    on_file, on_server = both_halves(sequence, tmp_path / "parity.db")
    assert on_file == on_server
    endpoint, updated, cleared, absent, gone, minds = on_file
    assert endpoint == "http://two:11434"
    assert updated == 2
    assert [cleared, absent] == [1, 0]
    assert gone is None
    assert minds == [("skippy", "standard", "sandboxed", "container")]


def test_the_two_halves_record_the_same_operation_requester(
        tmp_path: Path) -> None:
    """The console scopes an operation's status to whoever started it, so a
    half that lost the record answers "not yours" to the person who asked."""
    async def sequence() -> list[Any]:
        await store.record_operation_requester("req-1", "daniel", 1)
        await store.record_operation_requester("req-1", "sloan", 2)
        return [await store.operation_requester("req-1"),
                await store.operation_requester("never-recorded")]

    on_file, on_server = both_halves(sequence, tmp_path / "parity.db")
    assert on_file == on_server
    assert on_file == ["sloan", None]


def test_the_two_halves_guard_the_last_admin_the_same_way(
        tmp_path: Path) -> None:
    """The console refuses a change that would leave nobody able to
    administer the hive. The count is a statement on both halves, so a half
    that counted differently locks the operator out of their own console."""
    import auth

    async def sequence() -> list[Any]:
        first = await auth.create_user("daniel", "daniels-pass",
                                       is_admin=True)
        second = await auth.create_user("sloan", "sloans-pass",
                                        is_admin=True)
        account_store = auth.BuiltinStore()
        both = await account_store.enabled_members_besides_for_test(
            first["id"]) if hasattr(
                account_store, "enabled_members_besides_for_test") else (
            await store.enabled_members_besides(group_name=auth.ADMINS,
                                                user_id=first["id"]))
        await account_store.update_user("sloan", disabled=True)
        alone = await store.enabled_members_besides(
            group_name=auth.ADMINS, user_id=first["id"])
        refused = None
        try:
            await account_store.update_user("daniel", disabled=True)
        except auth.Conflict as exc:
            refused = str(exc)
        signed_in, _ = await account_store.sign_in("daniel", "daniels-pass")
        return [both, alone, refused, signed_in["is_admin"],
                sorted(group["name"]
                       for group in await account_store.list_groups()),
                second["username"]]

    on_file, on_server = both_halves(sequence, tmp_path / "parity.db")
    assert on_file == on_server
    both, alone, refused, is_admin, groups, created = on_file
    assert [both, alone] == [1, 0]
    assert refused == "The hive must keep at least one enabled admin."
    assert is_admin is True
    assert groups == ["admins"]
    assert created == "sloan"


def test_the_two_halves_sign_a_cookie_with_the_same_stored_key(
        tmp_path: Path) -> None:
    """The signing key is minted on first use and kept. Two reads handing
    back different keys is every cookie the console issued becoming
    invalid, which presents as the whole install being signed out."""
    import auth

    async def sequence() -> list[Any]:
        auth.forget_secret_key()
        first = await store.put_auth_setting_if_absent("secret_key", "mine")
        second = await store.put_auth_setting_if_absent("secret_key",
                                                        "somebody-elses")
        return [first, second, await store.get_auth_setting("secret_key"),
                await store.get_auth_setting("never-set")]

    environment = os.environ.pop("HIVE_SECRET_KEY", None)
    try:
        on_file, on_server = both_halves(sequence, tmp_path / "parity.db")
    finally:
        if environment is not None:
            os.environ["HIVE_SECRET_KEY"] = environment
    assert on_file == on_server
    assert on_file == ["mine", "mine", "mine", None]


# ---------------------------------------------------------------------------
# the memory browser


def test_the_two_halves_report_a_table_that_is_not_there_the_same_way(
        tmp_path: Path) -> None:
    """None is what the console turns into a 404. A half that raised instead
    would answer 500 to a typo in a URL, which reads as an outage."""
    async def sequence() -> list[Any]:
        return [await store.memory_rows("no_such_table_anywhere")]

    lucent = tmp_path / "lucent.db"
    import sqlite3

    connection = sqlite3.connect(lucent)
    connection.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    os.environ["LUCENT_DB_PATH"] = str(lucent)
    try:
        on_file, on_server = both_halves(sequence, tmp_path / "parity.db")
    finally:
        os.environ.pop("LUCENT_DB_PATH", None)
    assert on_file == on_server
    assert on_file == [None]


def test_the_postgres_half_reads_memory_out_of_the_one_database(
        tmp_path: Path) -> None:
    """Requirement three, and the window this cutover closes: a console on
    Postgres whose memory pages still read a file would answer correctly
    from a store nothing writes to any more.

    One half only, because the two read two different *databases* here —
    which is the whole point. What is compared is the shape they answer in.
    """
    if not ADMIN_DSN:
        pytest.skip(NO_SERVER)

    async def sequence() -> list[Any]:
        from hive_data import memory

        await memory.store(mind_id="a-mind", content="a memory on postgres",
                           embedding=[0.5] * 4096, created_at=1)
        tables = {row["name"]: row["row_count"]
                  for row in await store.memory_tables()}
        page = await store.memory_rows("memories")
        return [tables.get("memories"), page["total"], page["columns"],
                [row["content"] for row in page["rows"]],
                page["rows"][0]["embedding"]]

    count, total, columns, contents, embedding = asyncio.run(
        _on_postgres(sequence))
    assert count == 1
    assert total == 1
    assert "content" in columns and "embedding" in columns
    assert contents == ["a memory on postgres"]
    assert embedding == "<vector 4096 dims>"


# ---------------------------------------------------------------------------
# what one request's transaction may do to another's


def test_two_board_mutations_at_once_both_land(tmp_path: Path) -> None:
    """They take turns rather than one of them answering 500.

    On one shared connection the second `BEGIN IMMEDIATE` is an error rather
    than a wait, so two tabs dragging cards — or one double-tapped drop — is
    a refusal nobody can explain. Each unit holds its own connection, so the
    second waits out the file's write lock the way every caller did before
    this module existed.
    """
    async def sequence() -> list[Any]:
        async def create(name: str) -> str:
            return (await kanban_data.create_epic(name))["id"]

        first, second, third = await asyncio.gather(
            create("One"), create("Two"), create("Three"))
        titles = sorted(row["title"]
                        for row in await kanban_data.list_epics())
        positions = sorted(
            row["position"] for row in await store.work_items("epic"))
        return [titles, len({first, second, third}), len(set(positions))]

    on_file, on_server = both_halves(sequence, tmp_path / "parity.db")
    assert on_file == on_server
    titles, distinct_ids, distinct_positions = on_file
    assert titles == ["One", "Three", "Two"]
    assert distinct_ids == 3
    assert distinct_positions == 3, "two epics landed on one position"


def test_a_unit_refuses_a_second_task_reaching_into_it(
        tmp_path: Path) -> None:
    """A task created inside a unit inherits it, and must not use it.

    A transaction is one connection: two tasks writing through it can each
    roll back the other's committed-looking work. `hive_data`'s unit refuses
    this outright, and the SQLite half has to refuse it too — otherwise the
    same code silently works on one half and raises on the other, which is
    the one thing the revert lever must not do.
    """
    async def sequence() -> list[Any]:
        refusals: list[str] = []

        async def reaches_in() -> None:
            try:
                await store.create_work_item("e-inner", "epic", "inner", 9)
            except Exception as exc:  # noqa: BLE001 - the class differs
                refusals.append(type(exc).__name__)

        async with store.board_transaction():
            await store.create_work_item("e-outer", "epic", "outer", 1)
            await asyncio.ensure_future(reaches_in())

        return [len(refusals) == 1,
                sorted(row["title"]
                       for row in await kanban_data.list_epics())]

    on_file, on_server = both_halves(sequence, tmp_path / "parity.db")
    assert on_file == on_server
    refused, titles = on_file
    assert refused is True, "a child task wrote through its parent's unit"
    assert titles == ["outer"]


def test_a_store_that_never_opened_does_not_report_a_shutdown() -> None:
    """The two sentences send somebody to different places: a route running
    outside the app, or work that outlived one."""
    async def unopened() -> str:
        os.environ[store.DSN_VAR] = ""
        store._engine = None
        store._was_open = False
        try:
            await store.release_notice(1)
        except RuntimeError as exc:
            return str(exc)
        return "no refusal"

    said = asyncio.run(unopened())
    assert "never opened" in said


def test_a_unit_cancelled_waiting_for_the_file_lock_leaves_nothing_open(
        tmp_path: Path) -> None:
    """The SQLite half only, because the scenario is the file's write lock.

    A rival writer holds it — the terminal site runs this same module against
    this same file — so the unit blocks inside `BEGIN IMMEDIATE`, and the
    request is cancelled there. A browser tab closing does that now: the
    handlers are native coroutines, so there is no threadpool bridge left to
    absorb it.

    Shared, the connection was left inside a transaction with nobody to end
    it. The console then read back everything it wrote and lost all of it
    when the store closed, with only the next board mutation saying anything
    at all. The unit's connection is its own and is closed on the way out, so
    what follows is an ordinary store.
    """
    if not ADMIN_DSN:
        pytest.skip(NO_SERVER)

    db_path = tmp_path / "cancelled.db"

    async def sequence() -> list[Any]:
        import sqlite3

        await kanban_data.create_epic("Before")

        rival = sqlite3.connect(str(db_path), isolation_level=None)
        rival.execute("BEGIN IMMEDIATE")
        try:
            blocked = asyncio.ensure_future(_opens_a_unit())
            # Long enough to be inside the blocking BEGIN, short enough that
            # the ten-second busy timeout has not expired.
            await asyncio.sleep(0.2)
            blocked.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await blocked
        finally:
            rival.execute("ROLLBACK")
            rival.close()

        after = await kanban_data.create_epic("After")
        return [sorted(row["title"]
                       for row in await kanban_data.list_epics()),
                after["title"]]

    answered = asyncio.run(_on_sqlite(sequence, db_path))
    titles, after = answered
    assert titles == ["After", "Before"]
    assert after == "After"


async def _opens_a_unit() -> None:
    """A board mutation, for the cancellation test to interrupt."""
    async with store.board_transaction():
        await store.create_work_item("e-never", "epic", "never", 5)


def test_the_group_admin_is_exists_on_a_store_that_has_no_admin_yet(
        tmp_path: Path) -> None:
    """Both halves seed it as the store opens, as the console's own schema
    creation did on every open.

    Left to the first `create_user(is_admin=True)`, a store holding people
    and no admin yet has no such group: the groups page omits it, and adding
    the first admin to it answers "No such group" with nothing on screen
    saying the row is simply missing.
    """
    import auth

    async def sequence() -> list[Any]:
        account_store = auth.BuiltinStore()
        before = sorted(group["name"]
                        for group in await account_store.list_groups())
        await auth.create_user("somebody", "a-password")
        # The first admin is made by adding them to the group that is there.
        await account_store.add_member("admins", "somebody")
        return [before, (await account_store.get_user("somebody"))["groups"]]

    on_file, on_server = both_halves(sequence, tmp_path / "parity.db")
    assert on_file == on_server
    before, groups = on_file
    assert before == ["admins"]
    assert groups == ["admins"]


def test_a_nul_byte_in_a_board_field_is_refused_rather_than_stored(
        tmp_path: Path) -> None:
    """One paste, one answer, on both halves.

    SQLite holds a NUL byte in a text column quite happily and Postgres
    cannot encode one at all, so left to the engines the same paste is a
    stored row on one half and an unhandled error on the other. Refused
    rather than stripped: a requirement quietly altered on its way in is a
    document that does not say what was typed, with nothing on the page
    saying so.
    """
    async def sequence() -> list[Any]:
        epic = await kanban_data.create_epic("An epic")
        refusals = []
        for field, value in (("title", "a\x00b"),
                             ("description", "a\x00b")):
            try:
                await kanban_data.update_epic(epic["id"], **{field: value})
            except ValueError as exc:
                refusals.append((field, "NUL byte" in str(exc)))
        try:
            await kanban_data.create_epic("fine", requirements="a\x00b")
        except ValueError as exc:
            refusals.append(("requirements", "NUL byte" in str(exc)))
        after = await kanban_data.find_item(epic["id"])
        return [refusals, after["title"], after["description"],
                len(await kanban_data.list_epics())]

    on_file, on_server = both_halves(sequence, tmp_path / "parity.db")
    assert on_file == on_server
    refusals, title, description, epics = on_file
    assert refusals == [("title", True), ("description", True),
                        ("requirements", True)]
    assert title == "An epic"
    assert description == ""
    assert epics == 1, "a refused creation left a row behind"
