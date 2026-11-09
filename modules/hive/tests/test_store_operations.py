"""What the console's store operations answer, on the hive's Postgres.

`hive_data`'s own suite guards each operation against a real Postgres. What
these guard is the *wiring*: which `hive_data` function each store operation
calls, with which argument order, and which key it reads off the result
(`["rows"]`, `["row"]`). Each test runs one sequence of operations against the
suite's scratch database and asserts on what came back.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from hive_site.testing import make_conversations, run

import kanban_data
import store

# ---------------------------------------------------------------------------
# the board


def test_the_board_is_built_and_numbered() -> None:
    """The numbering is the risk the whole cutover carries: every story's
    number appears in conversations, commit messages and the lucent graph, so
    numbering that drifts is a port that renames work."""
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

    answered = run(sequence())
    epics, features, stories, numbers, one_number = answered
    assert epics == ["An epic"]
    assert features == ["A feature"]
    assert stories == ["First story", "Second story", "Under the epic"]
    assert numbers == ["1", "1.1", "1.1.1", "1.1.2", "1.2"]
    assert one_number == "1.1.2"


def test_a_drop_reorders_the_sibling_list() -> None:
    """A drop renumbers the whole sibling list."""
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

    answered = run(sequence())
    first, second = answered
    assert first == ["Story 2", "Story 0", "Story 1"]
    assert second == ["Story 2", "Story 1", "Story 0"]


def test_a_deleted_subtree_is_counted_as_what_went() -> None:
    """What went, not what the page predicted. The count is rendered."""
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

    answered = run(sequence())
    predicted, removed, left = answered
    assert predicted == {"features": 1, "stories": 2}
    assert removed == {"features": 1, "stories": 2}
    assert left == ["Elsewhere"]


def test_a_retype_changes_every_column_it_implies() -> None:
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

    answered = run(sequence())
    subtype, status, kind_now, epic_keys, kind = answered
    assert subtype == "bug"
    assert status == "backlog"
    assert kind_now == "bug"
    # An epic's record carries no parent key at all, whatever `parent_id`
    # holds — the board selects a feature by `epic_id` and would draw one.
    assert "epic_id" not in epic_keys and "parent_id" not in epic_keys
    assert kind == "epic"


async def _conversations(*ids: str) -> None:
    """Session rows for the conversations a design session will point at."""
    await make_conversations(*ids)


# ---------------------------------------------------------------------------
# design sessions and their notices


def test_a_page_write_is_guarded_on_its_revision() -> None:
    """The revision is in the statement, so the row count *is* the
    arbitration: getting it wrong is one browser tab's work
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

    answered = run(sequence())
    first, stale, revision, requirements = answered
    assert [first, stale] == [1, 0]
    assert revision == 2
    assert requirements == '{"r1": {}}'


def test_a_notice_is_claimed_once() -> None:
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
        # Released *after* delivery, which is the guard the other side of
        # this pair exists for: an acknowledgement racing a failure report
        # must not un-deliver a notice that actually landed, and a released
        # notice is one the next sweep sends again.
        un_delivered = await store.release_notice(notice_id)
        after = await store.notice(notice_id)
        return [
            won is not None and won["body"], lost, released,
            again is not None, delivered, twice, un_delivered,
            after is not None and after["claimed_at"] is not None,
            [row["kind"] for row in await store.pending_notices("d-1")],
        ]

    answered = run(sequence())
    (body, lost, released, again, delivered, twice, un_delivered,
     still_claimed, pending) = answered
    assert body == "go"
    assert lost is None
    assert [released, again, delivered, twice] == [1, True, 1, 0]
    assert un_delivered == 0, "a delivered notice was un-delivered"
    assert still_claimed is True, "a delivered notice lost its claim"
    assert pending == []


def test_an_attach_keeps_what_it_was_not_told_about() -> None:
    """A mind re-claims a session on every turn that touches it, knowing the
    conversation and nothing else. An attach that wrote null instead would
    blank the mind the page was published by, every turn."""
    async def sequence() -> list[Any]:
        # The conversations exist first: `design_sessions.conversation_id`
        # is a real foreign key into `core.sessions`.
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

    answered = run(sequence())
    assert answered == ["conv-1", "console", "conv-2", "telegram"]


def test_deleting_a_session_takes_its_notices_with_it() -> None:
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

    answered = run(sequence())
    assert answered == [1, 0, [], None]


# ---------------------------------------------------------------------------
# settings, operations and people


def test_a_capability_endpoint_and_an_edge_mind_are_replaced_not_duplicated() -> None:
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

    answered = run(sequence())
    endpoint, updated, cleared, absent, gone, minds = answered
    assert endpoint == "http://two:11434"
    assert updated == 2
    assert [cleared, absent] == [1, 0]
    assert gone is None
    assert minds == [("skippy", "standard", "sandboxed", "container")]


def test_the_last_recorded_operation_requester_is_the_one_answered() -> None:
    """The console scopes an operation's status to whoever started it, so a
    lost record answers "not yours" to the person who asked."""
    async def sequence() -> list[Any]:
        await store.record_operation_requester("req-1", "daniel", 1)
        await store.record_operation_requester("req-1", "sloan", 2)
        return [await store.operation_requester("req-1"),
                await store.operation_requester("never-recorded")]

    answered = run(sequence())
    assert answered == ["sloan", None]


def test_the_last_enabled_admin_is_guarded() -> None:
    """The console refuses a change that would leave nobody able to
    administer the hive. The count is one statement, and counting wrong locks the operator out of their own console."""
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

    answered = run(sequence())
    both, alone, refused, is_admin, groups, created = answered
    assert [both, alone] == [1, 0]
    assert refused == "The hive must keep at least one enabled admin."
    assert is_admin is True
    assert groups == ["admins"]
    assert created == "sloan"


def test_the_stored_signing_key_is_never_replaced() -> None:
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
        answered = run(sequence())
    finally:
        if environment is not None:
            os.environ["HIVE_SECRET_KEY"] = environment
    assert answered == ["mine", "mine", "mine", None]


# ---------------------------------------------------------------------------
# the memory browser


def test_a_memory_table_that_is_not_there_is_reported_as_none() -> None:
    """None is what the console turns into a 404. Raising instead would
    answer 500 to a typo in a URL, which reads as an outage."""
    async def sequence() -> list[Any]:
        return [await store.memory_rows("no_such_table_anywhere")]

    answered = run(sequence())
    assert answered == [None]


def test_the_memory_browser_reads_memory_out_of_the_one_database() -> None:
    """A console whose memory pages read anywhere else would answer from a
    store nothing writes to any more."""

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

    count, total, columns, contents, embedding = run(sequence())
    assert count == 1
    assert total == 1
    assert "content" in columns and "embedding" in columns
    assert contents == ["a memory on postgres"]
    assert embedding == "<vector 4096 dims>"


# ---------------------------------------------------------------------------
# what one request's transaction may do to another's


def test_two_board_mutations_at_once_both_land() -> None:
    """They take turns rather than one of them answering 500: two tabs
    dragging cards, or one double-tapped drop, must not be a refusal nobody
    can explain."""
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

    answered = run(sequence())
    titles, distinct_ids, distinct_positions = answered
    assert titles == ["One", "Three", "Two"]
    assert distinct_ids == 3
    assert distinct_positions == 3, "two epics landed on one position"


def test_a_unit_refuses_a_second_task_reaching_into_it() -> None:
    """A task created inside a unit inherits it, and must not use it.

    A transaction is one connection: two tasks writing through it can each
    roll back the other's committed-looking work.
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

    answered = run(sequence())
    refused, titles = answered
    assert refused is True, "a child task wrote through its parent's unit"
    assert titles == ["outer"]


def test_the_two_reasons_there_is_no_store_are_told_apart() -> None:
    """A straggling background task and a route outside the app are different
    sentences, and each sends somebody somewhere else.

    Both sides of the distinction, from a module in each state rather than
    from private globals set by hand: a reimported module has genuinely never
    opened, and a closed one genuinely has.
    """
    import importlib

    async def after_closing(module: Any) -> str:
        await module.open()
        await module.close()
        try:
            await module.release_notice(1)
        except RuntimeError as exc:
            return str(exc)
        return "no refusal"

    async def before_opening(module: Any) -> str:
        try:
            await module.release_notice(1)
        except RuntimeError as exc:
            return str(exc)
        return "no refusal"

    fresh = importlib.reload(importlib.import_module("store"))
    never = run(before_opening(fresh))
    closed = run(after_closing(fresh))
    # Every other test in this suite holds the module the rest of the console
    # imported, so it is put back as it was.
    importlib.reload(fresh)

    assert "never opened" in never, never
    assert "closed" in closed, closed
    assert "never opened" not in closed, closed


def test_the_group_admin_is_exists_on_a_store_that_has_no_admin_yet() -> None:
    """The store seeds it as it opens.

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

    answered = run(sequence())
    before, groups = answered
    assert before == ["admins"]
    assert groups == ["admins"]


def test_a_nul_byte_in_a_board_field_is_refused_rather_than_stored() -> None:
    """One paste, one answer.

    Postgres cannot encode a NUL byte at all, so left to the engine the
    paste is an unhandled error. Refused rather than stripped: a requirement quietly altered on its way in is a
    document that does not say what was typed, with nothing on the page
    saying so.
    """
    async def sequence() -> list[Any]:
        epic = await kanban_data.create_epic("An epic")
        fields = ("title", "description", "requirements",
                  "acceptance_criteria", "qa_tests")
        updates = []
        for field in fields:
            try:
                await kanban_data.update_epic(epic["id"],
                                              **{field: "a\x00b"})
            except ValueError as exc:
                updates.append((field, "NUL byte" in str(exc)))
        creates = []
        for field in fields:
            argument = {field: "a\x00b"} if field != "title" else {}
            title = "a\x00b" if field == "title" else "fine"
            try:
                await kanban_data.create_epic(title, **argument)
            except ValueError as exc:
                creates.append((field, "NUL byte" in str(exc)))
        after = await kanban_data.find_item(epic["id"])
        return [updates, creates, after["title"], after["description"],
                len(await kanban_data.list_epics())]

    answered = run(sequence())
    updates, creates, title, description, epics = answered
    expected = [(field, True) for field in
                ("title", "description", "requirements",
                 "acceptance_criteria", "qa_tests")]
    assert updates == expected
    assert creates == expected
    assert title == "An epic"
    assert description == ""
    assert epics == 1, "a refused creation left a row behind"


def test_a_unit_that_raises_leaves_none_of_its_writes_behind() -> None:
    """The whole reason the construct exists.

    Several writes, all of them or none: a unit that committed what it had
    done before it failed would leave the board holding half a move — a row
    under its new parent with the sibling list never renumbered.
    """
    class Deliberate(Exception):
        """Raised to fail the unit on purpose."""

    async def sequence() -> list[Any]:
        await kanban_data.create_epic("Survivor")
        try:
            async with store.board_transaction():
                await store.create_work_item("e-one", "epic", "one", 10)
                await store.create_work_item("e-two", "epic", "two", 11)
                raise Deliberate
        except Deliberate:
            pass
        return [sorted(row["title"]
                       for row in await kanban_data.list_epics()),
                await store.work_item("e-one"),
                await store.work_item("e-two")]

    answered = run(sequence())
    titles, one, two = answered
    assert titles == ["Survivor"]
    assert one is None and two is None


def test_a_unit_inside_a_unit_is_refused() -> None:
    """The inner block would end the outer one's work on its way out, and a
    failure after it would roll back nothing."""
    async def sequence() -> list[Any]:
        refused = None
        async with store.board_transaction():
            await store.create_work_item("e-outer", "epic", "outer", 1)
            try:
                async with store.transaction():
                    await store.create_work_item("e-inner", "epic",
                                                 "inner", 2)
            except RuntimeError as exc:
                refused = "nest" in str(exc)
        return [refused,
                sorted(row["title"]
                       for row in await kanban_data.list_epics())]

    answered = run(sequence())
    refused, titles = answered
    assert refused is True
    assert titles == ["outer"], "the refused inner unit wrote anyway"


# ---------------------------------------------------------------------------
# the reads and writes nothing else reaches


def test_a_person_and_their_groups_read_back_as_written() -> None:
    """Every read of a person, including the ones the console reaches on the
    accounts page and on every request that carries a cookie.

    Each of these is a `hive_data` call with its own argument order and its
    own result key.
    """
    import auth

    async def sequence() -> list[Any]:
        first = await auth.create_user("daniel", "daniels-pass",
                                       is_admin=True)
        await auth.create_user("sloan", "sloans-pass")
        by_name = await store.get_user("daniel")
        by_id = await store.get_user_by_id(first["id"])
        private = await store.get_user("daniel", private=True)
        await store.update_user(first["id"], email="D.Stewart@UTSystem.edu")
        found = await store.find_user(email="d.stewart@utsystem.EDU")
        missing = await store.find_user(domain_account="HIVE\\nobody")
        account_store = auth.BuiltinStore()
        await account_store.add_member("admins", "sloan")
        before = sorted(await store.groups_of(first["id"]))
        members = await store.group_usernames("admins")
        await store.remove_group_member(group_name="admins",
                                        user_id=first["id"])
        return [
            by_name["username"], by_id["username"],
            "password_hash" in by_name, "password_hash" in private,
            found is not None and found["username"],
            missing,
            [row["username"] for row in await store.list_users()],
            await store.count_users(),
            before, members,
            sorted(await store.groups_of(first["id"])),
            (await store.get_group("admins"))["name"],
            await store.get_group("no-such-group"),
        ]

    answered = run(sequence())
    (by_name, by_id, hash_in_plain, hash_in_private, found, missing, listed,
     counted, before, members, after, group, absent) = answered
    assert by_name == "daniel" and by_id == "daniel"
    assert hash_in_plain is False and hash_in_private is True
    assert found == "daniel"
    assert missing is None
    assert listed == ["daniel", "sloan"]
    assert counted == 2
    assert before == ["admins"]
    assert members == ["daniel", "sloan"]
    assert after == [], "the member was not removed"
    assert group == "admins"
    assert absent is None


def test_an_outside_identity_binds_moves_and_unbinds() -> None:
    """Microsoft sign-in.

    A binding read by the wrong key signs the wrong person in, and a binding
    that is written and then not found is a tenant where everybody's first
    sign-in creates a second account.
    """
    async def sequence() -> list[Any]:
        absent = await store.get_sso_binding(provider="microsoft",
                                             subject="oid-1")
        await store.put_sso_binding(provider="microsoft", subject="oid-1",
                                    username="daniel", created_at=1)
        bound = await store.get_sso_binding(provider="microsoft",
                                            subject="oid-1")
        # The same subject again, naming somebody else: the subject is the
        # key, so this moves the binding rather than adding a second.
        await store.put_sso_binding(provider="microsoft", subject="oid-1",
                                    username="sloan", created_at=2)
        moved = await store.get_sso_binding(provider="microsoft",
                                            subject="oid-1")
        # Two more subjects, so the listing has somebody in it to report:
        # every assertion below used to want an empty answer, which an
        # implementation returning nothing at all satisfies.
        await store.put_sso_binding(provider="microsoft", subject="oid-2",
                                    username="aiden", created_at=3)
        await store.put_sso_binding(provider="windows", subject="sid-1",
                                    username="daniel", created_at=4)
        bound_now = sorted(await store.sso_usernames("microsoft"))
        other_provider = sorted(await store.sso_usernames("windows"))
        unknown_provider = sorted(await store.sso_usernames("okta"))
        removed = await store.delete_sso_binding(provider="microsoft",
                                                 username="sloan")
        again = await store.delete_sso_binding(provider="microsoft",
                                               username="sloan")
        return [absent, bound["username"], bound["subject"],
                moved["username"], bound_now, other_provider,
                unknown_provider, removed, again,
                sorted(await store.sso_usernames("microsoft"))]

    answered = run(sequence())
    (absent, bound, subject, moved, bound_now, other, unknown, removed,
     again, left) = answered
    assert absent is None
    assert bound == "daniel" and subject == "oid-1"
    assert moved == "sloan"
    assert bound_now == ["aiden", "sloan"], "the bound accounts went missing"
    assert other == ["daniel"], "the other provider's binding went missing"
    assert unknown == []
    assert [removed, again] == [1, 0]
    assert left == ["aiden"]


def test_the_remaining_board_and_design_reads_answer_what_was_written() -> None:
    """The four board and design reads the other parity tests do not reach.

    `update_work_item` above all: it is the write behind every edit on the
    item page, and returning zero without writing would be a page that saves
    nothing and says it saved.
    """
    async def sequence() -> list[Any]:
        epic = await kanban_data.create_epic("An epic")
        changed = await store.update_work_item(
            epic["id"], {"title": "Renamed", "description": "why"})
        nothing = await store.update_work_item(epic["id"], {})
        missing = await store.update_work_item("e-nope", {"title": "no"})
        read = await store.work_item(epic["id"])

        await store.create_design_session("d-1", "A design", 1.0, 1.0)
        await store.create_design_session("d-2", "Another", 2.0, 2.0)
        listed = [row["title"] for row in await store.design_sessions()]
        notice_id = await store.queue_notice("d-1", "feedback",
                                             "requirements", 3.0, body="hm")
        one = await store.notice(notice_id)

        await store.set_edge_mind("skippy", "operator", "operator",
                                  "systemd", 4)
        named = await store.edge_mind("skippy")
        unknown = await store.edge_mind("nobody")
        return [changed, nothing, missing, read["title"], read["description"],
                listed, one["body"], one["delivered_at"],
                named["deployment"], unknown]

    answered = run(sequence())
    (changed, nothing, missing, title, description, listed, body, delivered,
     deployment, unknown) = answered
    assert changed == 1
    assert nothing == 0
    assert missing == 0, "an update named a row that is not there and wrote"
    assert title == "Renamed" and description == "why"
    # Most recently written first, which only a write moves.
    assert listed == ["Another", "A design"]
    assert body == "hm" and delivered is None
    assert deployment == "systemd"
    assert unknown is None


def test_a_design_session_naming_no_such_mind_is_refused() -> None:
    """`design_sessions.mind_id` and `conversation_id` are real foreign keys
    where the split files could not have them, and a mind re-claims a session
    on every turn that touches it. A Mind naming its short name where its
    UUID belongs used to store a row pointing nowhere; it has to come back as
    a bad reference rather than as a fault in the console.
    """
    import design_data

    async def sequence() -> list[Any]:
        said = []
        try:
            await design_data.create_design_session("A design",
                                                    mind_id="skippy")
        except design_data.DesignUnknownReference as exc:
            said.append(("create", "not a row this hive holds" in str(exc)))

        await _conversations("conv-real")
        record = await design_data.create_design_session(
            "Attached", conversation_id="conv-real")
        try:
            await design_data.attach_design_session(record["id"],
                                                    "conv-swept")
        except design_data.DesignUnknownReference as exc:
            said.append(("attach", "conversation" in str(exc)))
        still = await design_data.get_design_session(record["id"])
        return [said, still["conversation_id"]]

    said, conversation = run(sequence())
    assert said == [("create", True), ("attach", True)]
    assert conversation == "conv-real", "the refused attach moved the session"
