"""A mind's one key at the tools: who is calling, and who may not decide.

The keys here are registered through the real credential routes, as the
console registers the one a person pasted, and used on the real gated routes.
Only the transport is stubbed: hive-comms' far side and the far side of the
replayed call.
"""

from __future__ import annotations

import json
from collections.abc import Generator

import httpx
import pytest
from fastapi.testclient import TestClient

import hitl
from auth_api import generate_approver_token, generate_token
from tests.scratch import db, run

ADMIN = "tools-admin-for-tests"  # secret-guard: allow - invented fixture
ADA_KEY = "ada-pasted-key-0123456789"  # secret-guard: allow - invented fixture
BOB_KEY = "bob-pasted-key-0123456789"  # secret-guard: allow - invented fixture
ADA_ID = "565e5a66-d20c-4266-872a-3268c4c894fc"
BOB_ID = "39369381-0700-4203-b742-f6c9555adcc7"
COMMS = "http://comms.test:8424"
LABEL = {"name": "Receipts/2026"}


def _bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    from server import create_app

    monkeypatch.setenv("TOOLS_ADMIN_BEARER_TOKEN", ADMIN)
    with TestClient(create_app(), raise_server_exceptions=False) as entered:
        yield entered


def _register(client: TestClient, holder: str, value: str) -> httpx.Response:
    return client.put(f"/credentials/{holder}", json={"token": value},
                      headers=_bearer(ADMIN))


@pytest.fixture()
def comms(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """hive-comms' far side: a registry holding Ada and Bob, and a message sink."""
    monkeypatch.setenv("COMMS_URL", COMMS)
    monkeypatch.setenv("COMMS_BEARER_TOKEN", "service-token")
    posted: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/broker/minds":
            return httpx.Response(200, json=[
                {"id": BOB_ID, "name": "bob"}, {"id": ADA_ID, "name": "ada"}])
        if request.method == "POST" and request.url.path == "/broker/messages":
            posted.append(request)
            body = json.loads(request.content)
            return httpx.Response(200, json={
                "status": "dispatched",
                "conversation_id": body["conversation_id"],
                "message_id": body["message_id"]})
        return httpx.Response(404, json={"error": "no such route"})

    monkeypatch.setattr(hitl, "_COMMS_TRANSPORT", httpx.MockTransport(handler),
                        raising=False)
    return posted


def _tool_answers(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    replayed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        replayed.append(request)
        return httpx.Response(200, json={"id": "Label_1", "created": True})

    monkeypatch.setattr(hitl, "_REPLAY_TRANSPORT", httpx.MockTransport(handler),
                        raising=False)
    return replayed


def _ask(client: TestClient, key: str) -> str:
    asked = client.post("/gmail/labels", json=LABEL, headers=_bearer(key))
    assert asked.status_code == 202
    return asked.json()["hitl_request_id"]


def _approve(client: TestClient, request_id: str) -> httpx.Response:
    return client.post(f"/hitl/{request_id}/respond", json={"action": "approve"},
                       headers=_bearer(run(generate_approver_token())))


def test_a_registered_key_is_accepted_as_that_mind_and_its_gated_call_is_recorded_and_answered_to_it(
        client: TestClient, comms: list[httpx.Request],
        monkeypatch: pytest.MonkeyPatch) -> None:
    _tool_answers(monkeypatch)
    assert _register(client, "ada", ADA_KEY).status_code == 200
    assert _register(client, "bob", BOB_KEY).status_code == 200

    request_id = _ask(client, ADA_KEY)
    assert _approve(client, request_id).status_code == 200
    run(hitl.settle_replays(timeout=10))

    row = db.execute("SELECT caller_mind FROM hitl_requests WHERE id = ?",
                     (request_id,)).fetchone()
    assert row["caller_mind"] == "ada"
    sent = [json.loads(request.content) for request in comms]
    assert [(m["to"], m["from"]) for m in sent] == [(ADA_ID, ADA_ID)]


def test_another_minds_key_reads_the_status_of_a_request_but_never_its_outcome(
        client: TestClient, comms: list[httpx.Request],
        monkeypatch: pytest.MonkeyPatch) -> None:
    _tool_answers(monkeypatch)
    _register(client, "ada", ADA_KEY)
    _register(client, "bob", BOB_KEY)
    request_id = _ask(client, ADA_KEY)
    _approve(client, request_id)
    run(hitl.settle_replays(timeout=10))

    as_ada = client.get(f"/hitl/{request_id}", headers=_bearer(ADA_KEY)).json()
    as_bob = client.get(f"/hitl/{request_id}", headers=_bearer(BOB_KEY)).json()

    assert as_ada["outcome"]["result"]["id"] == "Label_1"
    assert as_bob["status"] == "approved"
    assert "outcome" not in as_bob


def test_no_mind_key_decides_an_approval_and_the_request_stays_pending(
        client: TestClient, comms: list[httpx.Request],
        monkeypatch: pytest.MonkeyPatch) -> None:
    replayed = _tool_answers(monkeypatch)
    _register(client, "ada", ADA_KEY)
    _register(client, "bob", BOB_KEY)
    request_id = _ask(client, ADA_KEY)

    answers = [
        client.post(f"/hitl/{request_id}/respond", json={"action": "approve"},
                    headers=_bearer(key)).status_code
        for key in (ADA_KEY, BOB_KEY)]
    run(hitl.settle_replays(timeout=10))

    assert answers == [403, 403]
    row = db.execute("SELECT status FROM hitl_requests WHERE id = ?",
                     (request_id,)).fetchone()
    assert row["status"] == "pending"
    assert replayed == []


def test_registering_a_value_another_holder_has_is_refused_naming_it_and_changes_nothing(
        client: TestClient) -> None:
    assert _register(client, "ada", ADA_KEY).status_code == 200

    refused = _register(client, "bob", ADA_KEY)

    assert refused.status_code == 400
    assert "'ada'" in refused.json()["error"]
    assert ADA_KEY not in refused.text
    assert client.get("/hitl/none", headers=_bearer(ADA_KEY)).status_code == 404
    assert client.get("/hitl/none", headers=_bearer(BOB_KEY)).status_code == 401


def test_a_value_another_holder_gave_up_is_still_refused_until_it_is_removed(
        client: TestClient) -> None:
    _register(client, "ada", ADA_KEY)
    client.delete("/credentials/ada", headers=_bearer(ADMIN))

    assert _register(client, "bob", ADA_KEY).status_code == 400
    removed = client.delete("/credentials/ada?remove=true", headers=_bearer(ADMIN))
    assert removed.json()["removed"] is True
    assert _register(client, "bob", ADA_KEY).status_code == 200


@pytest.mark.parametrize("holder", ["hitl-approver", " Hitl-Approver ", "internal",
                                    "INTERNAL", "skippy"])
def test_the_reserved_holders_are_refused_on_every_credential_route(
        client: TestClient, holder: str) -> None:
    put = _register(client, holder, "any-value-long-enough")
    delete = client.delete(f"/credentials/{holder}", headers=_bearer(ADMIN))

    assert (put.status_code, delete.status_code) == (400, 400)
    assert client.get("/credentials", headers=_bearer(ADMIN)).json() == []


def test_a_new_value_for_a_holder_replaces_the_old_one_which_stops_working(
        client: TestClient) -> None:
    _register(client, "ada", ADA_KEY)
    _register(client, "ada", "ada-second-key-0123456789")  # secret-guard: allow

    assert client.get("/hitl/none", headers=_bearer(ADA_KEY)).status_code == 401
    assert client.get("/hitl/none", headers=_bearer(
        "ada-second-key-0123456789")).status_code == 404  # secret-guard: allow


def test_a_holders_own_revoked_value_can_be_given_back_to_it(
        client: TestClient) -> None:
    _register(client, "ada", ADA_KEY)
    client.delete("/credentials/ada", headers=_bearer(ADMIN))
    assert client.get("/hitl/none", headers=_bearer(ADA_KEY)).status_code == 401

    assert _register(client, "ada", ADA_KEY).status_code == 200
    assert client.get("/hitl/none", headers=_bearer(ADA_KEY)).status_code == 404


def test_the_key_is_accepted_however_a_env_file_spelled_its_line_ending(
        client: TestClient) -> None:
    _register(client, "ada", ADA_KEY)

    for spelled in (f"{ADA_KEY}\r", f" {ADA_KEY} ", f"{ADA_KEY}\r\n"):
        answer = client.get("/hitl/none", headers={"Authorization": f"Bearer {spelled}"})
        assert answer.status_code == 404, repr(spelled)


def test_cutting_a_holder_off_makes_its_pending_request_undecidable(
        client: TestClient, comms: list[httpx.Request],
        monkeypatch: pytest.MonkeyPatch) -> None:
    replayed = _tool_answers(monkeypatch)
    _register(client, "ada", ADA_KEY)
    request_id = _ask(client, ADA_KEY)

    cut = client.delete("/credentials/ada", headers=_bearer(ADMIN))
    approved = _approve(client, request_id)
    run(hitl.settle_replays(timeout=10))

    assert cut.json()["expired_requests"] == 1
    assert approved.status_code == 409
    assert replayed == []


def test_the_credential_routes_answer_only_to_the_administrator_credential(
        client: TestClient) -> None:
    _register(client, "ada", ADA_KEY)
    service_token = run(generate_token("scheduler"))
    approver = run(generate_approver_token())

    for key in (ADA_KEY, service_token, approver, "wrong"):
        assert client.put("/credentials/bob", json={"token": BOB_KEY},
                          headers=_bearer(key)).status_code == 401, key
        assert client.get("/credentials", headers=_bearer(key)).status_code == 401, key
    assert client.get("/credentials").status_code == 401
    assert client.get("/hitl/none", headers=_bearer(BOB_KEY)).status_code == 401


def test_without_an_administrator_credential_configured_the_routes_are_off(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOOLS_ADMIN_BEARER_TOKEN")

    assert client.get("/credentials", headers=_bearer("")).status_code == 503
    assert client.get("/credentials", headers={"Authorization": "Bearer "}).status_code == 503


def test_the_listing_names_holders_and_whether_they_are_live_and_never_a_value(
        client: TestClient) -> None:
    _register(client, "ada", ADA_KEY)
    _register(client, "bob", BOB_KEY)
    client.delete("/credentials/bob", headers=_bearer(ADMIN))

    listed = client.get("/credentials", headers=_bearer(ADMIN))

    assert [(row["holder"], row["live"]) for row in listed.json()] == [
        ("ada", True), ("bob", False)]
    assert ADA_KEY not in listed.text and BOB_KEY not in listed.text
    assert "token_hash" not in listed.text


def test_a_registered_mind_of_someone_else_is_refused_before_any_tool_runs(
        client: TestClient, comms: list[httpx.Request]) -> None:
    import time as _time

    from hive_data import core

    import store

    async def seed() -> None:
        now = _time.time()
        await core.upsert_mind(
            mind_id="arnold-id", name="arnold", gateway_url="http://arnold:8420",
            model="sonnet", harness="claude_cli", registered_at=now,
            last_seen=now, owner="sloan")
        await store.create_user("daniel", "x", 1, int(now))

    run(seed())
    _register(client, "arnold", "arnold-pasted-key-0123456789")  # secret-guard: allow

    refused = client.post(
        "/docker/restart?path=/tmp/x",
        headers=_bearer("arnold-pasted-key-0123456789"))  # secret-guard: allow

    assert refused.status_code == 403
    assert "sloan" in refused.json()["detail"]
    assert db.execute("SELECT count(*) AS n FROM hitl_requests").fetchone()["n"] == 0


def test_cutting_a_holder_off_ends_its_sudo_grants_and_a_new_holder_of_the_name_starts_without(
        client: TestClient) -> None:
    import time as _time

    from hive_data import tools as _tools

    _register(client, "ada", ADA_KEY)
    now = int(_time.time())
    run(_tools.grant_sudo("gmail_create_label", "ada", now, now + 900))

    client.delete("/credentials/ada?remove=true", headers=_bearer(ADMIN))

    assert run(_tools.active_grant("gmail_create_label", "ada", now)) is None


def test_retiring_the_shared_token_refuses_it_and_leaves_every_other_holder_and_the_approver_standing(
        client: TestClient) -> None:
    shared = run(generate_token("skippy"))
    approver = run(generate_approver_token())
    _register(client, "ada", ADA_KEY)
    assert client.get("/hitl/none", headers=_bearer(shared)).status_code == 404

    retired = client.post("/credentials/shared-token/retire", headers=_bearer(ADMIN))

    assert retired.status_code == 200 and retired.json()["revoked"] is True
    assert client.get("/hitl/none", headers=_bearer(shared)).status_code == 401
    assert client.get("/hitl/none", headers=_bearer(ADA_KEY)).status_code == 404
    assert client.get("/credentials", headers=_bearer(approver)).status_code == 401
    again = client.post("/credentials/shared-token/retire", headers=_bearer(ADMIN))
    assert again.status_code == 200 and again.json()["revoked"] is False


def test_the_shared_tokens_holder_name_stays_reserved_until_it_is_retired_and_then_a_mind_may_take_it(
        client: TestClient) -> None:
    run(generate_token("skippy"))
    assert _register(client, "skippy", ADA_KEY).status_code == 400

    client.post("/credentials/shared-token/retire", headers=_bearer(ADMIN))

    assert _register(client, "skippy", ADA_KEY).status_code == 200
    assert client.get("/hitl/none", headers=_bearer(ADA_KEY)).status_code == 404


def test_retiring_the_shared_token_answers_only_to_the_administrator_credential(
        client: TestClient) -> None:
    shared = run(generate_token("skippy"))
    _register(client, "ada", ADA_KEY)

    for key in (shared, ADA_KEY, "wrong"):
        assert client.post("/credentials/shared-token/retire",
                           headers=_bearer(key)).status_code == 401, key
    assert client.get("/hitl/none", headers=_bearer(shared)).status_code == 404
