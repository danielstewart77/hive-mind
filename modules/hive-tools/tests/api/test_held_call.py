"""A gated call stays open while a person decides it (Daniel, 2026-11-27).

The mind's call is held on the tool's own API. When the owner or an
administrator approves, the same call is released and runs the tool, and the
tool's result is the reply to that call. When a person denies, or the wait
runs out, the same call is answered with a plain refusal. Nothing is replayed
later and no mind's key is kept.

Everything is the real app and the real gate on the scratch Postgres. Only
the Gmail client (where the process stops being ours) and the clock are
substituted.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

import hitl
import store
from tests.held import LABEL, Clock, an_app, bearer, the_waiting_request
from tests.scratch import RawSQL, generate_person_key, generate_token

WAIT = 2 * 60


def _gmail_that_creates_label_nine() -> MagicMock:
    service = MagicMock()
    service.users().labels().list().execute.return_value = {"labels": []}
    service.users().labels().create().execute.return_value = {
        "id": "Label_9", "name": "Receipts/2026"}
    service.users().labels().create.reset_mock()
    return service


async def _mind_and_people(db_conn: RawSQL) -> dict[str, str]:
    from hive_data import core

    now = time.time()
    await core.upsert_mind(mind_id="m-ada", name="ada", gateway_url="http://x",
                           model="m", harness="h", registered_at=now,
                           last_seen=now, owner="daniel")
    return {
        "ada": await generate_token("ada"),
        "daniel": await generate_person_key("daniel", admin=True),
        "erin": await generate_person_key("erin", admin=True),
        "sam": await generate_person_key("sam"),
        "mallory": await generate_person_key("mallory"),
    }


async def _hold_a_label_call(client: httpx.AsyncClient, keys: dict[str, str],
                             waited: float = 5.0):
    """Ada makes the gated call; returns it still running, and its request id."""
    call = asyncio.create_task(
        client.post("/gmail/labels", json=LABEL, headers=bearer(keys["ada"])))
    return call, await the_waiting_request(waited)


@pytest.fixture()
def held_for_five_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HITL_WAIT_SECONDS", str(WAIT))


async def test_an_approval_releases_the_held_call_and_the_tool_result_is_its_reply(
        db_conn: RawSQL, held_for_five_minutes: None) -> None:
    keys = await _mind_and_people(db_conn)
    _, client = await an_app()
    service = _gmail_that_creates_label_nine()
    with patch("tools.gmail._get_gmail_service", return_value=service):
        call, request_id = await _hold_a_label_call(client, keys)
        await asyncio.sleep(0.05)
        assert not call.done(), "the call must stay open until a person decides"
        service.users().labels().create.assert_not_called()

        decided = await client.post(
            f"/hitl/{request_id}/respond", json={"action": "approve"},
            headers=bearer(keys["daniel"]))
        reply = await asyncio.wait_for(call, 5)

    assert decided.status_code == 200
    assert reply.status_code == 200
    assert reply.json() == {"id": "Label_9", "name": "Receipts/2026",
                            "created": True}
    sent = service.users().labels().create.call_args.kwargs
    assert sent["body"]["name"] == "Receipts/2026"
    row = await store.request(request_id)
    assert (row["status"], row["decided_by"]) == ("approved", "daniel")


async def test_a_denial_answers_the_held_call_with_a_plain_no_and_the_tool_does_not_run(
        db_conn: RawSQL, held_for_five_minutes: None) -> None:
    keys = await _mind_and_people(db_conn)
    _, client = await an_app()
    service = _gmail_that_creates_label_nine()
    with patch("tools.gmail._get_gmail_service", return_value=service):
        call, request_id = await _hold_a_label_call(client, keys)
        await client.post(f"/hitl/{request_id}/respond",
                          json={"action": "deny"}, headers=bearer(keys["erin"]))
        reply = await asyncio.wait_for(call, 5)

    assert reply.status_code == 403
    assert reply.json()["approved"] is False
    assert reply.json()["decided_by"] == "erin"
    assert reply.json()["error"].startswith("Refused: erin denied this request")
    assert "did not run" in reply.json()["error"]
    service.users().labels().create.assert_not_called()


async def test_no_answer_in_time_refuses_the_held_call_and_the_tool_does_not_run(
        db_conn: RawSQL, held_for_five_minutes: None,
        monkeypatch: pytest.MonkeyPatch) -> None:
    keys = await _mind_and_people(db_conn)
    _, client = await an_app()
    clock = Clock(time.time())
    monkeypatch.setattr(hitl, "_now", clock.now)
    monkeypatch.setattr(hitl, "_pause", clock.pause)
    service = _gmail_that_creates_label_nine()
    started = clock.t
    with patch("tools.gmail._get_gmail_service", return_value=service):
        reply = await asyncio.wait_for(
            client.post("/gmail/labels", json=LABEL,
                        headers=bearer(keys["ada"])), 10)

    assert reply.status_code == 408
    assert reply.json()["status"] == "expired"
    assert reply.json()["approved"] is False
    assert f"within {WAIT} seconds" in reply.json()["error"]
    assert "timed out" in reply.json()["error"]
    # The wait was waited through, on the injected clock.
    assert clock.t - started >= WAIT
    service.users().labels().create.assert_not_called()


async def test_a_yes_that_arrives_after_the_time_is_out_is_refused_and_runs_nothing(
        db_conn: RawSQL, held_for_five_minutes: None,
        monkeypatch: pytest.MonkeyPatch) -> None:
    keys = await _mind_and_people(db_conn)
    _, client = await an_app()
    clock = Clock(time.time())
    monkeypatch.setattr(hitl, "_now", clock.now)
    monkeypatch.setattr(hitl, "_pause", clock.pause)
    service = _gmail_that_creates_label_nine()
    with patch("tools.gmail._get_gmail_service", return_value=service):
        reply = await asyncio.wait_for(
            client.post("/gmail/labels", json=LABEL,
                        headers=bearer(keys["ada"])), 10)
        request_id = (await store.recent_requests())[0]["id"]
        late = await client.post(
            f"/hitl/{request_id}/respond", json={"action": "approve"},
            headers=bearer(keys["daniel"]))

    assert reply.status_code == 408
    assert late.status_code == 409
    service.users().labels().create.assert_not_called()


async def test_the_pending_record_names_who_asked_what_and_when_and_who_decided(
        db_conn: RawSQL, held_for_five_minutes: None) -> None:
    keys = await _mind_and_people(db_conn)
    _, client = await an_app()
    with patch("tools.gmail._get_gmail_service",
               return_value=_gmail_that_creates_label_nine()):
        call, request_id = await _hold_a_label_call(client, keys)
        waiting = (await client.get("/hitl/waiting",
                                    headers=bearer(keys["daniel"]))).json()
        review = (await client.get(f"/hitl/{request_id}/review",
                                   headers=bearer(keys["daniel"]))).json()
        await client.post(f"/hitl/{request_id}/respond",
                          json={"action": "approve"},
                          headers=bearer(keys["daniel"]))
        await asyncio.wait_for(call, 5)
        after = (await client.get(f"/hitl/{request_id}",
                                  headers=bearer(keys["ada"]))).json()

    assert [r["hitl_request_id"] for r in waiting["waiting"]] == [request_id]
    assert (waiting["waiting"][0]["caller_mind"],
            waiting["waiting"][0]["tool_name"]) == ("ada", "gmail_create_label")
    assert review["status"] == "pending" and review["path"] == "/gmail/labels"
    assert after["status"] == "approved" and after["decided_by"] == "daniel"
    assert after["created_at"] and after["decided_at"]


@pytest.mark.parametrize("who", ["mallory", "sam", "ada"])
async def test_someone_who_may_not_decide_leaves_the_call_held(
        db_conn: RawSQL, held_for_five_minutes: None, who: str) -> None:
    """Another person, a non-administrator who owns nothing here, and the
    asking mind's own key are all refused, and the call stays open."""
    keys = await _mind_and_people(db_conn)
    _, client = await an_app()
    service = _gmail_that_creates_label_nine()
    with patch("tools.gmail._get_gmail_service", return_value=service):
        call, request_id = await _hold_a_label_call(client, keys)
        refused = await client.post(
            f"/hitl/{request_id}/respond", json={"action": "approve"},
            headers=bearer(keys[who]))
        await asyncio.sleep(0.05)
        still_held = not call.done()
        await client.post(f"/hitl/{request_id}/respond",
                          json={"action": "deny"},
                          headers=bearer(keys["daniel"]))
        await asyncio.wait_for(call, 5)

    assert refused.status_code == 403
    assert still_held
    service.users().labels().create.assert_not_called()


async def test_an_owner_who_is_no_longer_an_administrator_can_decide_but_the_call_is_refused(
        db_conn: RawSQL, held_for_five_minutes: None) -> None:
    """daniel owns ada; he is then taken out of the administrators, so only
    his owning ada lets him decide. Deleting the owner branch of `may_decide`
    turns his 200 into a 403. The integrations are an administrator's, so an
    approval by the owner of a mind that is no longer an administrator's lands
    denied, and the call is refused."""
    from hive_data import access, core

    keys = await _mind_and_people(db_conn)
    _, client = await an_app()
    service = _gmail_that_creates_label_nine()
    with patch("tools.gmail._get_gmail_service", return_value=service):
        call, request_id = await _hold_a_label_call(client, keys)
        daniel = next(u for u in await core.list_users()
                      if str(u["username"]) == "daniel")
        await core.remove_group_member(group_name=access.ADMINS_GROUP,
                                       user_id=daniel["id"])
        assert await access.person_is_administrator("daniel") is False
        decided = await client.post(
            f"/hitl/{request_id}/respond", json={"action": "approve"},
            headers=bearer(keys["daniel"]))
        reply = await asyncio.wait_for(call, 5)

    assert decided.status_code == 200 and decided.json()["decided_by"] == "daniel"
    assert reply.status_code == 403
    service.users().labels().create.assert_not_called()


async def test_a_call_whose_caller_hung_up_is_closed_so_a_late_yes_runs_nothing(
        db_conn: RawSQL, held_for_five_minutes: None) -> None:
    keys = await _mind_and_people(db_conn)
    _, client = await an_app()
    service = _gmail_that_creates_label_nine()
    with patch("tools.gmail._get_gmail_service", return_value=service):
        call, request_id = await _hold_a_label_call(client, keys)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        late = await client.post(
            f"/hitl/{request_id}/respond", json={"action": "approve"},
            headers=bearer(keys["daniel"]))

    assert late.status_code == 409
    service.users().labels().create.assert_not_called()


async def test_starting_the_tools_closes_requests_whose_held_call_died_with_the_last_process(
        db_conn: RawSQL) -> None:
    await _mind_and_people(db_conn)
    now = int(time.time())
    await store.create_request("orphan", "gmail_send_email", "ada", "{}", now,
                               now + 300, "POST", "/gmail/send", "{}")

    closed = await hitl.close_waiting_requests()

    assert closed == 1
    assert await store.waiting_requests(now) == []
    keys_person = await generate_person_key("daniel", admin=True)
    _, client = await an_app()
    late = await client.post("/hitl/orphan/respond", json={"action": "approve"},
                             headers=bearer(keys_person))
    assert late.status_code == 409


async def test_a_wait_of_zero_refuses_at_once_and_still_records_the_request(
        db_conn: RawSQL, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HITL_WAIT_SECONDS", "0")
    keys = await _mind_and_people(db_conn)
    _, client = await an_app()
    with patch("tools.gmail._get_gmail_service",
               return_value=_gmail_that_creates_label_nine()):
        reply = await asyncio.wait_for(
            client.post("/gmail/labels", json=LABEL,
                        headers=bearer(keys["ada"])), 5)

    assert reply.status_code == 408
    assert (await store.recent_requests())[0]["tool_name"] == "gmail_create_label"


@pytest.mark.parametrize("raw, expected", [
    ("", WAIT), ("soon", WAIT), ("-4", WAIT),
    ("90", 90), ("0", 0)])
def test_the_wait_is_a_setting_with_five_minutes_when_it_is_missing_or_unusable(
        monkeypatch: pytest.MonkeyPatch, raw: str, expected: int) -> None:
    monkeypatch.setenv("HITL_WAIT_SECONDS", raw)
    assert hitl.wait_seconds() == expected


async def test_in_sudo_mode_one_approval_lets_the_minds_next_call_through_without_being_held(
        db_conn: RawSQL, held_for_five_minutes: None,
        monkeypatch: pytest.MonkeyPatch) -> None:
    keys = await _mind_and_people(db_conn)
    await store.set_setting("gmail_create_label", "sudo", 15)
    _, client = await an_app()
    service = _gmail_that_creates_label_nine()
    with patch("tools.gmail._get_gmail_service", return_value=service):
        call, request_id = await _hold_a_label_call(client, keys)
        await client.post(f"/hitl/{request_id}/respond",
                          json={"action": "approve"},
                          headers=bearer(keys["daniel"]))
        assert (await asyncio.wait_for(call, 5)).status_code == 200
        # With a wait of zero, a second call that had to be held would be
        # refused; the grant is what lets it through.
        monkeypatch.setenv("HITL_WAIT_SECONDS", "0")
        second = await asyncio.wait_for(
            client.post("/gmail/labels", json=LABEL,
                        headers=bearer(keys["ada"])), 5)

    assert second.status_code == 200


async def test_a_mind_of_a_person_who_is_not_an_administrator_is_refused_before_any_request_is_held(
        db_conn: RawSQL, held_for_five_minutes: None) -> None:
    """The real refusal rule, unreplaced: sam is no administrator, so these
    integrations are not his and his mind's call is refused outright, never
    held for anyone to approve. Removing the rule turns this 403 into a held
    call."""
    from hive_data import core

    keys = await _mind_and_people(db_conn)
    now = time.time()
    await core.upsert_mind(mind_id="m-bo", name="bo", gateway_url="http://x",
                           model="m", harness="h", registered_at=now,
                           last_seen=now, owner="sam")
    bo = await generate_token("bo")
    _, client = await an_app()
    with patch("tools.gmail._get_gmail_service",
               return_value=_gmail_that_creates_label_nine()):
        refused = await asyncio.wait_for(
            client.post("/gmail/labels", json=LABEL, headers=bearer(bo)), 5)

    assert refused.status_code == 403
    assert "belongs to sam" in refused.json()["detail"]
    waiting = (await client.get("/hitl/waiting",
                                headers=bearer(keys["daniel"]))).json()["waiting"]
    assert waiting == []
