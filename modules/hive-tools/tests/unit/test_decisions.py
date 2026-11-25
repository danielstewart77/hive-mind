"""What happens when a person decides a gated call.

Every behaviour here was claimed by the cutover commit and had no test. The
mutation reviewer proved it: deciding an expired request, collapsing the
three reasons a decision can lose into one, returning nothing to replay,
revoking an already-revoked token and shortening the sudo window from
minutes to seconds each left the suite entirely green.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

import store
from hitl import UnknownAction, respond_to_hitl
from tests.scratch import RawSQL


def _pending(db_conn: RawSQL, request_id: str, *,
             tool: str = "docker_compose_up", expires_in: int = 300,
             body: str = '{"path": "/opt/thing"}') -> int:
    """A request waiting on a person, with the call it captured."""
    now = int(time.time())
    db_conn.execute(
        "INSERT INTO hitl_requests (id, tool_name, caller_mind, params_json, "
        "tool_method, tool_path, tool_body, status, created_at, expires_at) "
        "VALUES (?, ?, 'skippy', '{}', 'POST', '/docker/up', ?, 'pending', "
        "?, ?)",
        (request_id, tool, body, now, now + expires_in))
    db_conn.commit()
    return now


def _mode(db_conn: RawSQL, tool: str, mode: str,
          timeout_minutes: int = 15) -> None:
    db_conn.execute(
        "INSERT INTO tool_hitl_settings (tool_name, mode, "
        "sudo_timeout_minutes) VALUES (?, ?, ?)",
        (tool, mode, timeout_minutes))
    db_conn.commit()


async def test_a_request_past_its_expiry_cannot_be_approved(
        db_conn: RawSQL) -> None:
    """The expiry is half the decision's guard and had no test on this half.
    Approving a stale request would both mint the sudo grant and fire the
    replay - running a call whose approval window closed days ago."""
    _pending(db_conn, "stale", expires_in=-1)

    answer = await respond_to_hitl("stale", "approve")

    assert answer == {"error": "already_resolved",
                      "current_status": "expired"}
    row = db_conn.execute(
        "SELECT status, decided_at FROM hitl_requests WHERE id = 'stale'"
    ).fetchone()
    assert row["status"] == "pending"
    assert row["decided_at"] is None


async def test_the_three_reasons_a_decision_loses_are_told_apart(
        db_conn: RawSQL) -> None:
    """They have different remedies. No such request means the id is wrong;
    already settled means somebody else got there first; expired means the
    call has to be made again rather than approved."""
    assert await respond_to_hitl("no-such-request", "approve") is None

    _pending(db_conn, "settled")
    assert (await respond_to_hitl("settled", "deny"))["status"] == "denied"
    assert await respond_to_hitl("settled", "approve") == {
        "error": "already_resolved", "current_status": "denied"}

    _pending(db_conn, "timed-out", expires_in=-1)
    assert await respond_to_hitl("timed-out", "approve") == {
        "error": "already_resolved", "current_status": "expired"}


async def test_an_approved_decision_hands_back_the_call_to_replay(
        db_conn: RawSQL) -> None:
    """The replay is the whole point of capturing the call. Returned with the
    decision rather than re-read, so the loser of an arbitration does not run
    a second statement - and if it comes back empty, no approved call is ever
    re-executed and nothing anywhere says so."""
    _pending(db_conn, "live", body='{"path": "/opt/thing"}')

    answer = await respond_to_hitl("live", "approve")

    assert answer is not None
    assert answer["_replay"] == {"method": "POST", "path": "/docker/up",
                                 "body": '{"path": "/opt/thing"}'}


async def test_a_denied_decision_hands_back_nothing_to_replay(
        db_conn: RawSQL) -> None:
    """A denial is a refusal to run the call, so there is nothing to run."""
    _pending(db_conn, "refused")
    answer = await respond_to_hitl("refused", "deny")
    assert answer is not None
    assert "_replay" not in answer


async def test_a_sudo_grant_lasts_the_minutes_it_was_configured_for(
        db_conn: RawSQL) -> None:
    """Minutes, not seconds. A window short enough to close before the mind
    retries is a sudo mode that asks every time while reporting that it
    granted."""
    _mode(db_conn, "docker_compose_up", "sudo", timeout_minutes=20)
    _pending(db_conn, "sudo-one")

    before = int(time.time())
    await respond_to_hitl("sudo-one", "approve")

    grant = db_conn.execute(
        "SELECT granted_at, expires_at FROM sudo_grants "
        "WHERE tool_name = 'docker_compose_up'").fetchone()
    assert grant is not None
    assert grant["expires_at"] - grant["granted_at"] == 20 * 60
    assert grant["expires_at"] >= before + 20 * 60


async def test_a_decision_whose_grant_fails_leaves_the_request_decidable(
        db_conn: RawSQL) -> None:
    """The decision and the grant it earns are one unit of work.

    Committed separately they leave an end state nothing can recover from:
    the request says approved, the call it authorised was never replayed
    because the route fires the replay after this returns, and the request
    can never be approved again because the decision is guarded on pending.
    """
    _mode(db_conn, "docker_compose_up", "sudo")
    _pending(db_conn, "half-done")

    async def _fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the grant write went away")

    with patch.object(store, "grant_sudo", _fail):
        with pytest.raises(RuntimeError):
            await respond_to_hitl("half-done", "approve")

    row = db_conn.execute(
        "SELECT status, decided_at FROM hitl_requests WHERE id = 'half-done'"
    ).fetchone()
    assert row["status"] == "pending"
    assert row["decided_at"] is None
    assert db_conn.execute(
        "SELECT count(*) AS c FROM sudo_grants").fetchone()["c"] == 0

    # And because nothing settled, the decision can be made again.
    assert (await respond_to_hitl("half-done", "approve"))["status"] == \
        "approved"


async def test_an_action_that_is_neither_approve_nor_deny_is_refused(
        db_conn: RawSQL) -> None:
    """It used to deny. `{"action": "approved"}` - the natural past-tense
    mistake - denied the request irrevocably and answered 200 saying denied,
    and so did "yes" and an empty string."""
    _pending(db_conn, "typo")

    for action in ("approved", "yes", "", "APPROVE"):
        with pytest.raises(UnknownAction):
            await respond_to_hitl("typo", action)

    assert db_conn.execute(
        "SELECT status FROM hitl_requests WHERE id = 'typo'"
    ).fetchone()["status"] == "pending"


async def test_approving_a_request_from_another_persons_mind_denies_it(
        db_conn: RawSQL) -> None:
    """DECIDED 2026-11-15 (Daniel): his integrations are his minds' only. A
    request raised before that was enforced, or before the mind changed
    hands, is denied at the decision and never replayed."""
    from hive_data import core

    now = time.time()
    await core.upsert_mind(mind_id="arnold-id", name="arnold",
                           gateway_url="http://arnold:8420", model="m",
                           harness="claude_cli", registered_at=now,
                           last_seen=now, owner="sloan")
    _pending(db_conn, "from-arnold")
    db_conn.execute("UPDATE hitl_requests SET caller_mind = 'arnold' "
                    "WHERE id = 'from-arnold'")
    db_conn.commit()

    answer = await respond_to_hitl("from-arnold", "approve")

    assert answer["status"] == "denied"
    assert not answer.get("replay")
