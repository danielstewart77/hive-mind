"""The operations the console's cutover needs, which nothing else asked for.

Three groups, and each one exists because the console is the first service
to reach for it: the record of who started an operation, the people and
groups the built-in sign-in keeps, and the memory browser's table listing.

Every assertion reads what the operation produced. Where a write is the
behaviour, the row is read back on a plain driver connection as well, for
the reason the round-trip suite gives: a layer that writes and reads the same
wrong column consistently passes its own round trip.
"""

from __future__ import annotations

import uuid

import pytest

from hive_data import board, core, memory
from hive_data.errors import UnsettableColumn, ValueRefused


def _name(prefix: str) -> str:
    """A name of this test's own.

    The scratch database is made once per run, so rows outlive the test that
    wrote them - and `list_users` reads every row there is, so a test sharing
    a name with another is a test asserting on somebody else's people.
    """
    return f"{prefix}{uuid.uuid4().hex[:12]}"


# --------------------------------------------------- who started an operation

async def test_the_requester_of_an_operation_comes_back_by_request_id(
        connected, inspector):
    """The console scopes an operation's status to whoever started it, so the
    name it recorded has to be the name it reads back."""
    request_id = _name("req-")

    await board.record_operation_requester(request_id, "daniel", 1_700_000_000)

    recorded = await board.operation_requester(request_id)
    assert recorded is not None
    assert recorded["username"] == "daniel"
    assert recorded["created_at"] == 1_700_000_000
    assert await inspector.fetchval(
        'SELECT username FROM board."operation_requesters" '
        "WHERE request_id = $1", request_id) == "daniel"


async def test_an_operation_nobody_recorded_belongs_to_nobody(connected):
    """None is what the route table turns into "only an admin may read this".
    An empty dict would be a requester with no name, which reads as a row."""
    assert await board.operation_requester(_name("req-")) is None


async def test_recording_the_same_operation_twice_keeps_the_later_name(
        connected):
    """A tool reporting one request id twice - a replay, a re-POSTed webhook -
    must not turn the second report into a refused write on a path whose
    whole job is bookkeeping."""
    request_id = _name("req-")

    await board.record_operation_requester(request_id, "daniel", 1_700_000_000)
    await board.record_operation_requester(request_id, "sloan", 1_700_000_900)

    recorded = await board.operation_requester(request_id)
    assert recorded is not None
    assert recorded["username"] == "sloan"
    assert recorded["created_at"] == 1_700_000_900


# ------------------------------------------------------------ people and groups

async def test_a_password_hash_is_withheld_until_it_is_asked_for(connected):
    """Sign-in needs the hash and every other read of a person is rendered on
    a page. A hash that travels by default is a hash in a log."""
    username = _name("person-")
    await core.create_user(username=username,
                           password_hash="a-hash-the-test-invented",
                           created_at=1_700_000_000)

    plain = await core.get_user(username)
    assert plain is not None
    assert "password_hash" not in plain

    private = await core.get_user(username, private=True)
    assert private is not None
    assert private["password_hash"] == "a-hash-the-test-invented"


async def test_a_person_is_found_by_the_id_their_cookie_carries(connected):
    """`user_for_token` reads the id out of a signed cookie, not the name:
    a rename must not sign somebody out, and two people cannot share an id."""
    username = _name("person-")
    created = await core.create_user(
        username=username, password_hash="", created_at=1_700_000_000)

    found = await core.get_user_by_id(created["row"]["id"])
    assert found is not None
    assert found["username"] == username


async def test_an_outside_identity_is_matched_whatever_its_case(connected):
    """Microsoft hands back an email in whatever case the directory holds, and
    the account was typed by a person. A case-sensitive match is a new account
    for somebody who already has one."""
    username = _name("person-")
    await core.create_user(username=username, password_hash="",
                           created_at=1_700_000_000,
                           email="Daniel.Stewart@UTSystem.edu")

    found = await core.find_user(email="daniel.stewart@utsystem.EDU")
    assert found is not None
    assert found["username"] == username


async def test_finding_a_person_by_both_identities_at_once_is_refused(
        connected):
    """An email and a domain account that name two different people is a
    question with no answer, and whichever was checked first would sign one
    person in as the other."""
    with pytest.raises(ValueRefused):
        await core.find_user(email="a@b.c", domain_account="HIVE\\someone")


async def test_an_identity_that_matches_nothing_is_nobody(connected):
    """A miss is None rather than a raise: an unknown Windows principal is an
    ordinary first sign-in, which the console turns into an account."""
    assert await core.find_user(domain_account=_name("HIVE\\")) is None


async def test_the_groups_a_person_is_in_come_back_by_name(connected):
    """Membership is read on every request to decide what a page may show,
    and the only thing read off the row is the group's name."""
    username = _name("person-")
    group = _name("group-")
    created = await core.create_user(
        username=username, password_hash="", created_at=1_700_000_000)
    await core.create_group(name=group, created_at=1_700_000_000)
    await core.add_group_member(group_name=group,
                                user_id=created["row"]["id"])

    assert await core.groups_of(created["row"]["id"]) == [group]
    assert await core.group_usernames(group) == [username]


async def test_a_group_nobody_created_is_not_a_group(connected):
    """The console refuses a membership change against a group that is not
    there rather than creating one by writing a member into it."""
    assert await core.get_group(_name("group-")) is None


async def test_the_last_enabled_member_of_a_group_is_counted_in_the_database(
        connected):
    """The guard that keeps one admin enabled. Two admins disabling each other
    at the same moment both read a list that still holds the other, so the
    count has to be a statement inside the caller's own unit."""
    group = _name("group-")
    await core.create_group(name=group, created_at=1_700_000_000)
    first = await core.create_user(username=_name("person-"),
                                   password_hash="",
                                   created_at=1_700_000_000)
    second = await core.create_user(username=_name("person-"),
                                    password_hash="",
                                    created_at=1_700_000_000)
    for person in (first, second):
        await core.add_group_member(group_name=group,
                                    user_id=person["row"]["id"])

    assert await core.enabled_members_besides(
        group_name=group, user_id=first["row"]["id"]) == 1

    await core.update_user(second["row"]["id"], disabled_at=1_700_000_900)

    assert await core.enabled_members_besides(
        group_name=group, user_id=first["row"]["id"]) == 0


async def test_bumping_a_session_version_moves_it_by_one_from_whatever_it_held(
        connected, inspector):
    """A disable or a password reset has to end a session that is open right
    now. A caller that read the version, added one and wrote it back would
    lose the other writer's bump - which is the one case the column exists
    for."""
    created = await core.create_user(
        username=_name("person-"), password_hash="",
        created_at=1_700_000_000, session_version=4)

    report = await core.bump_session_version(created["row"]["id"])

    assert report["rows"] == 1
    assert report["row"]["session_version"] == 5
    assert await inspector.fetchval(
        'SELECT session_version FROM core."users" WHERE id = $1',
        created["row"]["id"]) == 5


async def test_everybody_with_an_account_is_listed_by_name(connected):
    """The accounts page is a list in name order, so the order is the
    operation's rather than whichever order the rows were written in."""
    late = _name("zzz-person-")
    early = _name("aaa-person-")
    for username in (late, early):
        await core.create_user(username=username, password_hash="",
                               created_at=1_700_000_000)

    listed = [row["username"] for row in await core.list_users()]
    assert listed.index(early) < listed.index(late)
    assert "password_hash" not in (await core.list_users())[0]


async def test_an_install_with_no_accounts_counts_none(connected, inspector):
    """What decides whether a fresh install seeds its first admin. A count
    that was wrong by one either locks the operator out or opens the console
    to the next person who loads the page."""
    before = await core.count_users()
    await core.create_user(username=_name("person-"), password_hash="",
                           created_at=1_700_000_000)
    assert await core.count_users() == before + 1


async def test_an_outside_identity_is_unbound_by_the_account_it_named(
        connected, inspector):
    """The operator detaching a Microsoft sign-in knows whose account it is
    and does not know the subject the directory issued."""
    username = _name("person-")
    subject = _name("subject-")
    await core.put_sso_binding(provider="microsoft", subject=subject,
                               username=username, created_at=1_700_000_000)

    assert username in await core.sso_usernames("microsoft")

    removed = await core.delete_sso_binding(provider="microsoft",
                                            username=username)

    assert removed["rows"] == 1
    assert await inspector.fetchval(
        'SELECT count(*) FROM core."sso_bindings" WHERE username = $1',
        username) == 0


async def test_unbinding_an_account_that_was_never_bound_removes_nothing(
        connected):
    """Reported as nothing removed rather than as a success, so the route can
    say "there was no binding" instead of implying it undid one."""
    removed = await core.delete_sso_binding(provider="microsoft",
                                            username=_name("person-"))
    assert removed["rows"] == 0


# ------------------------------------------------------------- memory browser

async def test_the_memory_browser_lists_the_schema_s_own_tables_with_counts(
        connected):
    """The page exists to be looked at after a load, so the number beside a
    table is a count rather than the planner's estimate of one."""
    listed = await memory.tables()
    by_name = {row["name"]: row["row_count"] for row in listed}

    assert "memories" in by_name
    assert "nodes" in by_name
    before = by_name["memories"]

    await memory.store(mind_id=_name("mind-"), content="a memory to count",
                       embedding=[0.0] * 4096, created_at=1_700_000_000)

    after = {row["name"]: row["row_count"] for row in await memory.tables()}
    assert after["memories"] == before + 1


async def test_a_table_the_memory_schema_does_not_have_is_not_browsable(
        connected):
    """None is what the console turns into a 404. A name that matches nothing
    is a typo in a URL, and a browser that raised would read as an outage."""
    assert await memory.rows("core_users_but_spelled_wrong") is None
    # Nor is a table in another schema reachable by naming it.
    assert await memory.rows("users") is None


async def test_a_browsed_row_summarises_what_no_page_can_render(connected):
    """An embedding is 4096 floats and the searchable column is the content
    again as lexeme positions. The SQLite browser showed `<BLOB n bytes>`;
    this one summarises in the database, so the bytes never cross the wire."""
    await memory.store(mind_id=_name("mind-"), content="a memory to browse",
                       embedding=[0.25] * 4096, created_at=1_700_000_000)

    page = await memory.rows("memories", limit=1000)
    assert page is not None
    assert "embedding" in page["columns"]
    assert "content_search" in page["columns"]

    mine = [row for row in page["rows"]
            if row["content"] == "a memory to browse"]
    assert mine, "the memory just stored was not on the page"
    assert mine[0]["embedding"] == "<vector 4096 dims>"
    assert mine[0]["content_search"].startswith("<tsvector ")


async def test_a_browsed_page_asks_for_no_more_than_the_ceiling(connected):
    """`memories` carries 4096 floats a row. A page has no paging beyond this
    ceiling, so a larger number asked for is a longer page rather than more
    information - and the answer says what it actually used."""
    page = await memory.rows("memories", limit=10_000, offset=-5)
    assert page is not None
    assert page["limit"] == memory.BROWSE_LIMIT_MAX
    assert page["offset"] == 0


async def test_two_pages_of_a_browsed_table_do_not_overlap(connected):
    """SQLite's browser named no order and got away with it. Postgres promises
    nothing, so paging without one repeats some rows and skips others - which
    on a browser built to check a load reads as a bad load."""
    for index in range(6):
        await memory.store(mind_id=_name("mind-"), content=f"paging {index}",
                           embedding=[float(index)] * 4096,
                           created_at=1_700_000_000 + index)

    total = (await memory.rows("memories", limit=1))["total"]
    assert total >= 6

    seen: list[int] = []
    for offset in range(0, total, 2):
        page = await memory.rows("memories", limit=2, offset=offset)
        assert page is not None
        seen.extend(row["id"] for row in page["rows"])

    assert len(seen) == len(set(seen)), "a row was served on two pages"
    assert len(seen) == total, "a row was skipped between pages"


# ----------------------------------------------------------------- retyping

async def test_a_retype_moves_every_column_the_new_kind_decides_at_once(
        connected, inspector):
    """A retype is one fact made of five columns. Written one at a time the
    board is briefly a feature carrying a story's status, or a row at a
    number another row already holds, and the console reads it in between."""
    epic = _name("e")
    story = _name("s")
    await board.create_work_item(epic, "epic", "an epic", 1)
    await board.create_work_item(story, "story", "a story", 2,
                                 parent_id=epic, status="backlog")

    await board.retype(story, "feature", status=None, subtype="",
                       parent_id=epic, position=9)

    row = await inspector.fetchrow(
        'SELECT "kind", "status", "subtype", "parent_id", "position" '
        'FROM board."work_items" WHERE "id" = $1', story)
    assert row["kind"] == "feature"
    assert row["status"] is None
    assert row["subtype"] == ""
    assert row["parent_id"] == epic
    assert row["position"] == 9


async def test_a_retype_to_a_kind_the_board_has_no_lane_for_is_refused(
        connected):
    """The three kinds are the board's whole vocabulary; a fourth is a row
    that renders in none of the three lanes."""
    item = _name("s")
    await board.create_work_item(item, "story", "a story", 1)
    with pytest.raises(ValueRefused):
        await board.retype(item, "milestone", position=2)


async def test_a_retype_cannot_make_an_item_its_own_parent(connected):
    """The board walks down from its roots, so a row pointing at itself is a
    row that renders nowhere and a recursive read that never ends."""
    item = _name("s")
    await board.create_work_item(item, "story", "a story", 1)
    with pytest.raises(ValueRefused):
        await board.retype(item, "story", parent_id=item, position=2)


async def test_an_update_cannot_change_what_an_item_is(connected):
    """The kind is not something a form field sets in passing: the title and
    the prose come off a page the operator typed into, and a `kind` arriving
    with them would retype the row with none of the four columns that follow
    from it."""
    item = _name("s")
    await board.create_work_item(item, "story", "a story", 1)
    with pytest.raises(UnsettableColumn):
        await board.update_work_item(item, {"kind": "epic"})
