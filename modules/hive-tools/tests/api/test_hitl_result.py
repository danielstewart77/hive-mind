"""What an approved call did, reported back to the mind that asked.

The gated call is made through the real route, approved through the real
route, and replayed by the real `hitl.py` onto the scratch Postgres. Only
the transport is stubbed: the far side of the replayed HTTP call, where it
leaves this process, and the HTTP to hive-comms.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import hitl
from auth_api import generate_approver_token, generate_token
from tests.scratch import run

ADA_ID = "565e5a66-d20c-4266-872a-3268c4c894fc"
COMMS = "http://comms.test:8424"
LABEL = {"name": "Receipts/2026"}

Answer = Callable[[httpx.Request], httpx.Response]


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    from server import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as entered:
        yield entered


@pytest.fixture()
def ada(client: TestClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {run(generate_token('ada'))}"}


def _comms_answers(monkeypatch: pytest.MonkeyPatch,
                   message_status: int = 200) -> list[httpx.Request]:
    """hive-comms' far side: a registry holding Ada, and a message sink."""
    monkeypatch.setenv("COMMS_URL", COMMS)
    monkeypatch.setenv("COMMS_BEARER_TOKEN", "service-token")
    posted: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/broker/minds":
            return httpx.Response(200, json=[
                {"id": "39369381-0700-4203-b742-f6c9555adcc7",
                 "name": "dragoman"},
                {"id": ADA_ID, "name": "ada"},
            ])
        if request.method == "POST" and request.url.path == "/broker/messages":
            posted.append(request)
            if message_status != 200:
                return httpx.Response(message_status, json={"error": "down"})
            body = json.loads(request.content)
            return httpx.Response(200, json={
                "status": "dispatched",
                "conversation_id": body["conversation_id"],
                "message_id": body["message_id"]})
        return httpx.Response(404, json={"error": "no such route"})

    monkeypatch.setattr(hitl, "_COMMS_TRANSPORT", httpx.MockTransport(handler),
                        raising=False)
    return posted


@pytest.fixture()
def comms(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    return _comms_answers(monkeypatch)


def _tool_answers(monkeypatch: pytest.MonkeyPatch,
                  answer: Answer) -> list[httpx.Request]:
    """The replayed call's far side answers with `answer`."""
    replayed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        replayed.append(request)
        return answer(request)

    monkeypatch.setattr(hitl, "_REPLAY_TRANSPORT", httpx.MockTransport(handler),
                        raising=False)
    return replayed


def _label_created(label_id: str) -> Answer:
    """Gmail's label route, answering only the call Daniel approved."""
    def answer(request: httpx.Request) -> httpx.Response:
        if (request.method == "POST" and request.url.path == "/gmail/labels"
                and json.loads(request.content) == LABEL
                and request.headers["Authorization"].startswith("Internal ")):
            return httpx.Response(200, json={
                "id": label_id, "name": LABEL["name"], "created": True})
        return httpx.Response(404, json={"detail": "not the approved call"})
    return answer


def _approver() -> dict[str, str]:
    """The credential the surface bots send when Daniel taps Approve."""
    return {"Authorization": f"Bearer {run(generate_approver_token())}"}


def _approve(client: TestClient, request_id: str) -> None:
    decided = client.post(f"/hitl/{request_id}/respond",
                          json={"action": "approve"}, headers=_approver())
    assert decided.status_code == 200
    run(hitl.settle_replays(timeout=10))


def _approved_label_request(client: TestClient,
                            ada: dict[str, str]) -> str:
    """Ada asks for a label, which is gated, and Daniel approves it."""
    asked = client.post("/gmail/labels", json=LABEL, headers=ada)
    assert asked.status_code == 202
    request_id = asked.json()["hitl_request_id"]
    _approve(client, request_id)
    return request_id


def test_an_approved_replay_that_succeeds_reports_success_and_the_tools_body(
        client: TestClient, ada: dict[str, str], comms: list[httpx.Request],
        monkeypatch: pytest.MonkeyPatch) -> None:
    label_id = f"Label_{uuid.uuid4().hex[:8]}"
    _tool_answers(monkeypatch, _label_created(label_id))

    request_id = _approved_label_request(client, ada)
    status = client.get(f"/hitl/{request_id}", headers=ada)

    assert status.status_code == 200
    answer = status.json()
    assert answer["status"] == "approved"
    assert answer["outcome"]["success"] is True
    assert answer["outcome"]["http_status"] == 200
    assert answer["outcome"]["result"]["id"] == label_id


@pytest.mark.parametrize("answer, error", [
    # A tool that caught its own failure answers 200 and says so, in each
    # of the shapes this service's routes say it in.
    (lambda request: httpx.Response(
        200, json={"error": "Gmail not authenticated"}),
     "Gmail not authenticated"),
    (lambda request: httpx.Response(200, json={
        "ok": False, "code": "lucent_error", "detail": "type exists"}),
     "type exists"),
    (lambda request: httpx.Response(200, json={
        "status": "error", "return_code": 1, "stdout": "",
        "stderr": "no such service: web"}),
     "no such service: web"),
    (lambda request: httpx.Response(
        500, json={"detail": "label service exploded"}),
     "label service exploded"),
], ids=["200-error", "200-ok-false", "200-status-error", "500"])
def test_an_approved_replay_that_fails_reports_failure_with_the_tools_error(
        client: TestClient, ada: dict[str, str], comms: list[httpx.Request],
        monkeypatch: pytest.MonkeyPatch, answer: Answer, error: str) -> None:
    _tool_answers(monkeypatch, answer)

    request_id = _approved_label_request(client, ada)
    outcome = client.get(f"/hitl/{request_id}", headers=ada).json()["outcome"]

    assert outcome["success"] is False
    assert error in outcome["error"]


def _refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def _timed_out(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("read timed out", request=request)


@pytest.mark.parametrize("answer, error", [
    (_refused, "connection refused"),
    (_timed_out, "did not answer"),
], ids=["refused", "timed-out"])
def test_a_replay_that_never_gets_an_answer_reports_failure(
        client: TestClient, ada: dict[str, str], comms: list[httpx.Request],
        monkeypatch: pytest.MonkeyPatch, answer: Answer, error: str) -> None:
    _tool_answers(monkeypatch, answer)

    request_id = _approved_label_request(client, ada)
    outcome = client.get(f"/hitl/{request_id}", headers=ada).json()["outcome"]

    assert outcome["success"] is False
    assert outcome["http_status"] is None
    assert error in outcome["error"]


@pytest.mark.parametrize("succeeds", [True, False], ids=["success", "failure"])
def test_the_outcome_is_sent_once_to_the_asking_mind_through_comms(
        client: TestClient, ada: dict[str, str], comms: list[httpx.Request],
        monkeypatch: pytest.MonkeyPatch, succeeds: bool) -> None:
    label_id = f"Label_{uuid.uuid4().hex[:8]}"
    refusal = f"quota exceeded {uuid.uuid4().hex[:6]}"
    _tool_answers(monkeypatch, _label_created(label_id) if succeeds else (
        lambda request: httpx.Response(200, json={"error": refusal})))

    request_id = _approved_label_request(client, ada)

    assert len(comms) == 1
    sent: dict[str, Any] = json.loads(comms[0].content)
    assert comms[0].headers["Authorization"] == "Bearer service-token"
    assert sent["to"] == ADA_ID
    assert sent["from"] == ADA_ID
    assert sent["metadata"]["request_type"] == "hitl_result"
    assert sent["metadata"]["hitl_request_id"] == request_id
    assert sent["metadata"]["tool_name"] == "gmail_create_label"
    assert sent["metadata"]["success"] is succeeds
    assert request_id in sent["content"]
    carried = label_id if succeeds else refusal
    assert carried in sent["metadata"]["result"]
    assert carried in sent["content"]
    if not succeeds:
        assert refusal in sent["metadata"]["error"]


def test_a_message_comms_refuses_never_loses_the_stored_outcome(
        client: TestClient, ada: dict[str, str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    posted = _comms_answers(monkeypatch, message_status=503)
    label_id = f"Label_{uuid.uuid4().hex[:8]}"
    _tool_answers(monkeypatch, _label_created(label_id))

    request_id = _approved_label_request(client, ada)
    outcome = client.get(f"/hitl/{request_id}", headers=ada).json()["outcome"]

    assert len(posted) == 1
    assert outcome["success"] is True
    assert outcome["result"]["id"] == label_id


def test_a_linkedin_post_is_captured_so_its_approval_replays_it(
        client: TestClient, ada: dict[str, str], comms: list[httpx.Request],
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The one gated route that called the gate by hand, without capturing
    the call - so every approved post had nothing to replay."""
    import tools.linkedin as linkedin

    token = tmp_path / "linkedin_token.json"
    token.write_text(json.dumps({"access_token": "tok",
                                 "expires_at": 9999999999, "user_id": "u1"}))
    monkeypatch.setattr(linkedin, "_TOKEN_PATH", token)
    post = {"content": f"hello {uuid.uuid4().hex[:6]}"}
    replayed = _tool_answers(monkeypatch, lambda request: httpx.Response(
        200, json={"ok": True, "post_id": "urn:li:share:1"}))

    asked = client.post("/linkedin/post", json=post, headers=ada)
    assert asked.status_code == 202
    request_id = asked.json()["hitl_request_id"]
    _approve(client, request_id)

    assert [(r.method, r.url.path) for r in replayed] == [
        ("POST", "/linkedin/post")]
    assert json.loads(replayed[0].content) == post
    outcome = client.get(f"/hitl/{request_id}", headers=ada).json()["outcome"]
    assert outcome["success"] is True


def test_a_replay_still_running_at_shutdown_is_recorded_as_cut_off(
        client: TestClient, ada: dict[str, str], comms: list[httpx.Request],
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Left running, it reaches a closed store and the request stays approved
    with no outcome for ever - which reads as a replay still running."""
    import asyncio

    async def hangs(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, json={"id": "too-late"})

    _tool_answers(monkeypatch, hangs)  # type: ignore[arg-type]
    asked = client.post("/gmail/labels", json=LABEL, headers=ada)
    request_id = asked.json()["hitl_request_id"]
    client.post(f"/hitl/{request_id}/respond", json={"action": "approve"},
                headers=_approver())

    run(hitl.settle_replays(timeout=0.2))
    outcome = client.get(f"/hitl/{request_id}", headers=ada).json()["outcome"]

    assert outcome["success"] is False
    assert outcome["error"] == hitl.CUT_OFF_ERROR
    assert len(comms) == 1


def test_a_mind_approving_its_own_gated_request_is_refused_and_it_stays_pending(
        client: TestClient, ada: dict[str, str], comms: list[httpx.Request],
        monkeypatch: pytest.MonkeyPatch) -> None:
    replayed = _tool_answers(monkeypatch, _label_created("Label_never"))
    asked = client.post("/gmail/labels", json=LABEL, headers=ada)
    request_id = asked.json()["hitl_request_id"]

    refused = client.post(f"/hitl/{request_id}/respond",
                          json={"action": "approve"}, headers=ada)
    run(hitl.settle_replays(timeout=10))

    assert refused.status_code == 403
    assert "A mind cannot decide its own gate" in refused.json()["detail"]
    status = client.get(f"/hitl/{request_id}", headers=ada)
    assert status.status_code == 202
    assert status.json()["status"] == "pending"
    assert replayed == []


def test_the_approver_credential_approves_a_gated_request_and_it_is_replayed(
        client: TestClient, ada: dict[str, str], comms: list[httpx.Request],
        monkeypatch: pytest.MonkeyPatch) -> None:
    label_id = f"Label_{uuid.uuid4().hex[:8]}"
    replayed = _tool_answers(monkeypatch, _label_created(label_id))
    asked = client.post("/gmail/labels", json=LABEL, headers=ada)
    request_id = asked.json()["hitl_request_id"]

    decided = client.post(f"/hitl/{request_id}/respond",
                          json={"action": "approve"}, headers=_approver())
    run(hitl.settle_replays(timeout=10))

    assert decided.status_code == 200
    assert decided.json()["status"] == "approved"
    assert [(r.method, r.url.path) for r in replayed] == [
        ("POST", "/gmail/labels")]
    outcome = client.get(f"/hitl/{request_id}", headers=ada).json()["outcome"]
    assert outcome["success"] is True
    assert outcome["result"]["id"] == label_id


def test_the_approver_credential_cannot_make_a_gated_call_of_its_own(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    replayed = _tool_answers(monkeypatch, _label_created("Label_never"))

    asked = client.post("/gmail/labels", json=LABEL, headers=_approver())

    assert asked.status_code == 403
    assert "hitl_request_id" not in asked.json()
    assert replayed == []
