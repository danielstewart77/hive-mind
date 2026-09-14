"""The console's half of the Sentinel triage review.

The event store is on the sensor box; this console holds none of it. So what
these prove is that Daniel's decisions actually reach the sensor, and that a
sensor which is unreachable or unconfigured reads as a problem rather than as
an empty queue — "nothing to review" and "I could not ask" must never render
the same, because the first one invites him to close the tab.
"""

import os
import sys

import httpx
import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth
import main as main_mod
import sentinel_review

SENSOR = "http://192.168.4.16:8431"


def _authed_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = auth.create_user("daniel", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
    return client


class StubSensor:
    """A sensor whose review queue lives in a dict instead of in SQLite."""

    def __init__(self):
        self.seen: list[tuple[str, str, dict | None]] = []
        self.reviewed: set[int] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json as _json

        path = request.url.path
        body = _json.loads(request.content or b"{}") if request.content else None
        self.seen.append((request.method, path, body))
        if request.headers.get("Authorization") != "Bearer sensor-token":
            return httpx.Response(401, json={"ok": False, "error": "bearer token required"})
        if path == "/review":
            rules = [
                {"id": 7, "class_slug": "zeek_redis_error", "name": "known noise",
                 "action_kind": "record_only", "applied_count": 38}
            ]
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "classes": [],
                    "rules": [r for r in rules if r["id"] not in self.reviewed],
                },
            )
        if path == "/review/mark":
            if body.get("kind") != "rule":
                # This stub implements the rule path only, so a class mark
                # exercises the "sensor has no such route" reading.
                return httpx.Response(404, json={"ok": False, "error": "no route"})
            self.reviewed.add(int(body["id"]))
            return httpx.Response(200, json={"ok": True, "kind": body["kind"], "id": body["id"]})
        if path == "/review/overturn":
            return httpx.Response(200, json={"ok": True, "rule_id": body["rule_id"],
                                             "retired": bool(body.get("retire"))})
        return httpx.Response(404, json={"ok": False, "error": "no route"})


@pytest.fixture
def sensor(monkeypatch):
    stub = StubSensor()
    monkeypatch.setenv("SENTINEL_REVIEW_URL", SENSOR)
    monkeypatch.setenv("SENTINEL_REVIEW_TOKEN", "sensor-token")
    transport = httpx.MockTransport(stub.handler)
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(sentinel_review.httpx, "AsyncClient", patched)
    return stub


def test_review_page_reads_the_sensors_unreviewed_rules(tmp_path, monkeypatch, sensor):
    client = _authed_client(tmp_path, monkeypatch)

    body = client.get("/api/sentinel/review").json()

    assert [r["id"] for r in body["rules"]] == [7]
    assert ("GET", "/review", None) in sensor.seen


def test_marking_a_rule_reviewed_reaches_the_sensor_and_drops_it_from_the_listing(
    tmp_path, monkeypatch, sensor
):
    client = _authed_client(tmp_path, monkeypatch)
    assert [r["id"] for r in client.get("/api/sentinel/review").json()["rules"]] == [7]

    marked = client.post("/api/sentinel/review/mark", json={"kind": "rule", "id": 7})

    assert marked.status_code == 200
    assert ("POST", "/review/mark", {"kind": "rule", "id": 7, "note": None}) in sensor.seen
    assert client.get("/api/sentinel/review").json()["rules"] == []


def test_overturning_a_rule_carries_the_verdict_to_the_sensor(tmp_path, monkeypatch, sensor):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.post("/api/sentinel/review/overturn", json={"rule_id": 7, "retire": True})

    assert response.status_code == 200 and response.json()["retired"] is True
    assert (
        "POST",
        "/review/overturn",
        {"rule_id": 7, "action_kind": None, "retire": True, "note": None},
    ) in sensor.seen


def test_an_unconfigured_sensor_is_refused_rather_than_read_as_an_empty_queue(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("SENTINEL_REVIEW_URL", raising=False)
    monkeypatch.setenv("SENTINEL_REVIEW_TOKEN", "sensor-token")
    client = _authed_client(tmp_path, monkeypatch)

    response = client.get("/api/sentinel/review")

    assert response.status_code == 503


def test_an_unreachable_sensor_is_reported_as_unreachable(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_REVIEW_URL", SENSOR)
    monkeypatch.setenv("SENTINEL_REVIEW_TOKEN", "sensor-token")

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(refuse)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        sentinel_review.httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(*a, **{**kw, "transport": transport}),
    )
    client = _authed_client(tmp_path, monkeypatch)

    response = client.get("/api/sentinel/review")

    assert response.status_code == 502


def test_the_review_page_is_behind_the_login(tmp_path, monkeypatch, sensor):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    auth.create_user("daniel", "secret-pass", is_admin=True, replace=True)
    anonymous = TestClient(main_mod.app, follow_redirects=False)

    assert anonymous.get("/sentinel").status_code in (302, 303, 307)
    assert anonymous.get("/api/sentinel/review").status_code in (401, 403)


def test_a_console_with_no_token_is_refused_rather_than_asking_unauthenticated(
    tmp_path, monkeypatch, sensor
):
    """The other half of "unconfigured".

    Sending with no credential gets the sensor's 401 back, which reads as
    "your credentials were rejected" for what is actually "you did not
    configure this console" — the wrong-machine diagnosis this module exists
    to avoid.
    """
    monkeypatch.delenv("SENTINEL_REVIEW_TOKEN", raising=False)
    client = _authed_client(tmp_path, monkeypatch)

    response = client.get("/api/sentinel/review")

    assert response.status_code == 503
    assert sensor.seen == []


def test_a_sensor_without_the_review_api_reads_as_an_old_build_not_a_missing_row(
    tmp_path, monkeypatch, sensor
):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.post("/api/sentinel/review/mark", json={"kind": "class", "id": 3})

    # The stub answers 404 on any route it does not implement.
    assert response.status_code == 501
    assert "without the review API" in response.json()["detail"]


def test_an_unknown_review_kind_is_refused_here_rather_than_relayed(
    tmp_path, monkeypatch, sensor
):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.post("/api/sentinel/review/mark", json={"kind": "everything", "id": 7})

    assert response.status_code == 400
    assert not any(path == "/review/mark" for _method, path, _body in sensor.seen)
