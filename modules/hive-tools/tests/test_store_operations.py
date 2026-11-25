"""What the store's operations answer, on the hive's Postgres.

`hive_data`'s own suite guards each `tools` operation against a real
Postgres. What these guard is the *wiring*: which `hive_data` function each
store operation calls, with which argument order, and which key it reads off
the result (`["rows"]`, `["row"]`). Each test runs one sequence of operations
against the suite's scratch database and asserts on what came back.
"""

from __future__ import annotations

from typing import Any

import pytest

import store
from tests.scratch import run


@pytest.fixture(autouse=True)
def _store_open():
    run(store.open())
    try:
        yield
    finally:
        run(store.close())


def test_a_request_is_decided_once_and_hands_back_its_call() -> None:
    """The decision, its guard, and what it hands back to replay."""
    async def sequence() -> list[Any]:
        at = 1_800_000_000
        await store.create_request(
            "req-live", "docker_compose_up", "skippy", "{}", at, at + 300,
            "POST", "/docker/up", '{"path": "/opt/thing"}')
        await store.create_request(
            "req-stale", "docker_compose_up", "skippy", "{}", at, at + 300,
            "POST", "/docker/up", "{}")

        won = await store.decide_request("req-live", "approved", at + 1, at)
        lost = await store.decide_request("req-live", "denied", at + 2, at)
        expired = await store.decide_request(
            "req-stale", "approved", at + 999, at + 999)
        missing = await store.decide_request("nope", "approved", at, at)
        return [won, lost, expired, missing,
                await store.request("req-live"),
                await store.request("req-stale")]

    answered = run(sequence())
    won, lost, expired, missing, live, stale = answered
    assert won is not None and won["tool_body"] == '{"path": "/opt/thing"}'
    assert lost is None and expired is None and missing is None
    assert live["status"] == "approved"
    assert stale["status"] == "pending"


def test_a_token_is_revoked_once() -> None:
    """The count is the arbitration, and it is the thing a caller branches
    on - getting it wrong is a revocation reported as a repeat, or the other
    way round."""
    async def sequence() -> list[Any]:
        at = 1_800_000_000
        await store.create_token("console", "digest-one", at)
        token = await store.live_token("digest-one")
        first = await store.revoke_token(token["id"], at + 1)
        second = await store.revoke_token(token["id"], at + 2)
        return [token["name"], first, second,
                await store.live_token("digest-one"),
                await store.revoke_token(9999, at + 3)]

    answered = run(sequence())
    assert answered == ["console", 1, 0, None, 0]


def test_seeding_a_mode_never_replaces_one() -> None:
    """Insert-if-absent, and the count is which tools were new. A replacing
    write re-arms every approval gate the operator
    turned off, on every restart."""
    async def sequence() -> list[Any]:
        first = await store.seed_setting("gmail_send_email", "on", 15)
        await store.set_setting("gmail_send_email", "off", 60)
        second = await store.seed_setting("gmail_send_email", "on", 15)
        return [first, second, await store.setting("gmail_send_email"),
                await store.setting("never_registered"),
                await store.settings()]

    answered = run(sequence())
    first, second, edited, absent, listing = answered
    assert [first, second] == [1, 0]
    assert edited == {"tool_name": "gmail_send_email", "mode": "off",
                      "sudo_timeout_minutes": 60}
    assert absent is None
    assert listing == [edited]


def test_the_queue_lists_newest_first_without_the_captured_bodies() -> None:
    """The row shape the approvals page renders. The captured bodies are not
    in it: on the live data the newest fifty rows carry megabytes of them,
    some holding credentials, and the page shows none of it. The replay
    reads them by name instead."""
    async def sequence() -> list[Any]:
        at = 1_800_000_000
        for index in range(3):
            await store.create_request(
                f"req-{index}", "gmail_send_email", "skippy",
                '{"secret": "hunter2"}', at + index, at + 300,
                "POST", "/gmail/send", '{"token": "hunter2"}')
        listing = await store.recent_requests()
        return [sorted(listing[0]), [row["id"] for row in listing],
                [row["id"] for row in await store.recent_requests(limit=2)]]

    answered = run(sequence())
    columns, ids, capped = answered
    # Every column of a request except the three the access layer declares
    # private - the captured body, which is what holds the credentials, and
    # the body the replayed tool answered with.
    assert columns == ["caller_mind", "created_at", "decided_at",
                       "expires_at", "id", "outcome_at", "outcome_ok",
                       "outcome_status", "status", "tool_method",
                       "tool_name", "tool_path"]
    # Newest first, and the tie-break is the id so the same list comes back
    # twice. Three requests one second apart, so no tie here - the ordering
    # that matters is the direction.
    assert ids == ["req-2", "req-1", "req-0"]
    assert capped == ["req-2", "req-1"]


def test_a_sudo_grant_runs_out_at_its_expiry() -> None:
    """The window's boundary. A grant expiring at this instant has run out,
    which is strictly-after or a mind is let through a gate
    one second after its grant died."""
    async def sequence() -> list[Any]:
        at = 1_800_000_000
        await store.grant_sudo("docker_compose_up", "skippy", at, at + 900)
        live = await store.active_grant("docker_compose_up", "skippy", at)
        boundary = await store.active_grant(
            "docker_compose_up", "skippy", at + 900)
        other_mind = await store.active_grant(
            "docker_compose_up", "mordecai", at)
        return [live["expires_at"], boundary, other_mind]

    answered = run(sequence())
    assert answered == [1_800_000_000 + 900, None, None]


def test_a_login_reads_back_as_written() -> None:
    """Every authenticated page load resolves a cookie to a user id and reads
    the login back, so a wrong read here is every session on the service."""
    async def sequence() -> list[Any]:
        at = 1_800_000_000
        created = await store.create_user("daniel", "argon2-hash", 1, at)
        by_name = await store.user_by_username("daniel")
        by_id = await store.user_by_id(created["id"])
        return [sorted(created), created["username"], created["is_admin"],
                by_name == created, by_id == created,
                await store.user_by_username("nobody"),
                await store.user_by_id(9999),
                await store.count_users()]

    answered = run(sequence())
    columns, username, is_admin, by_name, by_id, no_name, no_id, count = \
        answered
    assert columns == ["created_at", "disabled_at", "id", "is_admin",
                       "password_hash", "username"]
    assert (username, is_admin) == ("daniel", 1)
    assert by_name and by_id
    assert no_name is None and no_id is None
    assert count == 1


def test_a_unit_of_work_is_undone_together() -> None:
    """The construct the decision depends on. A transaction that committed
    anyway would leave the half-applied state the unit exists to prevent."""
    class Deliberate(Exception):
        pass

    async def sequence() -> list[Any]:
        at = 1_800_000_000
        try:
            async with store.transaction():
                await store.create_request(
                    "rolled-back", "gmail_send_email", "skippy", "{}", at,
                    at + 300, "POST", "/gmail/send", "{}")
                await store.grant_sudo("gmail_send_email", "skippy", at,
                                       at + 900)
                raise Deliberate
        except Deliberate:
            pass

        async with store.transaction():
            await store.create_request(
                "committed", "gmail_send_email", "skippy", "{}", at,
                at + 300, "POST", "/gmail/send", "{}")
        return [await store.request("rolled-back") is None,
                await store.active_grant("gmail_send_email", "skippy", at),
                await store.request("committed") is not None]

    answered = run(sequence())
    assert answered == [True, None, True]
