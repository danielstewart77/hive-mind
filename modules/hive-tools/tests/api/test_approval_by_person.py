"""B18: a request is approved by the owner of the mind that asked or by an
administrator, and a decision by anyone else, or with a mind's key, is
refused; the person's name is recorded and the call is replayed as before."""

from __future__ import annotations

import time
from collections.abc import Generator

import httpx
import pytest
from fastapi.testclient import TestClient

import hitl
from tests.scratch import generate_person_key, generate_token, run

LABEL = {"name": "Receipts/2026"}


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    from server import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as entered:
        yield entered


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture()
def asked(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """Ada, owned by daniel (an administrator), asks for a gated call; returns
    who holds what. The rule that refuses a mind of a non-administrator is the
    real one here, not replaced: it is why the mind is daniel's."""
    from hive_data import core

    now = time.time()
    run(core.upsert_mind(mind_id="m-ada", name="ada", gateway_url="http://x",
                         model="m", harness="h", registered_at=now,
                         last_seen=now, owner="daniel"))
    keys = {
        "ada": run(generate_token("ada")),
        "sam": run(generate_person_key("sam")),
        "daniel": run(generate_person_key("daniel", admin=True)),
        "erin": run(generate_person_key("erin", admin=True)),
        "mallory": run(generate_person_key("mallory")),
    }
    replayed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        replayed.append(request)
        return httpx.Response(200, json={"id": "Label_1"})

    monkeypatch.setattr(hitl, "_REPLAY_TRANSPORT", httpx.MockTransport(handler),
                        raising=False)
    monkeypatch.setattr(hitl, "_COMMS_TRANSPORT", httpx.MockTransport(
        lambda r: httpx.Response(404)), raising=False)
    sent = client.post("/gmail/labels", json=LABEL, headers=_bearer(keys["ada"]))
    assert sent.status_code == 202
    assert sent.json()["approve_at"].endswith(sent.json()["hitl_request_id"])
    return keys, sent.json()["hitl_request_id"], replayed


def test_a_mind_of_a_person_who_is_not_an_administrator_is_refused_before_any_approval_is_raised(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real refusal rule, unreplaced: sam is no administrator, so these
    integrations are not his and his mind's call is refused outright, never
    sent to anyone to approve. Removing the rule turns this 403 into a 202."""
    from hive_data import core

    now = time.time()
    run(core.upsert_mind(mind_id="m-bo", name="bo", gateway_url="http://x",
                         model="m", harness="h", registered_at=now,
                         last_seen=now, owner="sam"))
    run(generate_person_key("sam"))
    bo = run(generate_token("bo"))

    refused = client.post("/gmail/labels", json=LABEL, headers=_bearer(bo))

    assert refused.status_code == 403
    assert "belongs to sam" in refused.json()["detail"]
    assert "hitl_request_id" not in refused.json()
    daniel = run(generate_person_key("daniel", admin=True))
    waiting = client.get("/hitl/waiting", headers=_bearer(daniel)).json()["waiting"]
    assert waiting == []


@pytest.mark.parametrize("who", ["daniel", "erin"])
def test_the_owner_and_an_administrator_decide_and_their_name_is_recorded(
        client: TestClient, asked, who: str) -> None:
    keys, request_id, replayed = asked

    decided = client.post(f"/hitl/{request_id}/respond",
                          json={"action": "approve"}, headers=_bearer(keys[who]))
    run(hitl.settle_replays(timeout=10))

    assert decided.status_code == 200
    assert decided.json()["decided_by"] == who
    assert [(r.method, r.url.path) for r in replayed] == [("POST", "/gmail/labels")]
    seen = client.get(f"/hitl/{request_id}/review", headers=_bearer(keys[who])).json()
    assert (seen["status"], seen["decided_by"]) == ("approved", who)


@pytest.mark.parametrize("who", ["mallory", "sam", "ada"])
def test_another_person_or_a_minds_key_is_refused_and_sees_nothing(
        client: TestClient, asked, who: str) -> None:
    keys, request_id, replayed = asked

    refused = client.post(f"/hitl/{request_id}/respond",
                          json={"action": "approve"}, headers=_bearer(keys[who]))
    seen = client.get(f"/hitl/{request_id}/review", headers=_bearer(keys[who]))
    run(hitl.settle_replays(timeout=10))

    assert refused.status_code == 403
    assert seen.status_code == 403
    assert "Receipts" not in seen.text
    assert replayed == []
    status = client.get(f"/hitl/{request_id}", headers=_bearer(keys["ada"]))
    assert status.json()["status"] == "pending"


def test_the_waiting_list_holds_only_what_the_person_may_decide(
        client: TestClient, asked) -> None:
    keys, request_id, _ = asked

    def waiting(who: str) -> list[str]:
        got = client.get("/hitl/waiting", headers=_bearer(keys[who])).json()
        return [row["hitl_request_id"] for row in got["waiting"]]

    assert waiting("daniel") == [request_id]
    assert waiting("erin") == [request_id]
    assert waiting("sam") == []
    assert waiting("mallory") == []
