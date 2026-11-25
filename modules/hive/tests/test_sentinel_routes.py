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
import accounts

SENSOR = "http://192.168.4.16:8431"


def _authed_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = accounts.make_user("daniel", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, accounts.session_token(user))
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
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    accounts.make_user("daniel", "secret-pass", is_admin=True, replace=True)
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


# ---------------------------------------------------------------------------
# The bucket catalogue
# ---------------------------------------------------------------------------


_CATALOGUE = {
    "ok": True,
    "build": "abc123def456",
    "total_events": 48036,
    "last_intake": "2026-10-21 01:30:10",
    "remainder": {
        "on_retired_classes": 749,
        "on_classes_outside_the_nine": 0,
        "on_no_class": 0,
    },
    "supplied_facts": [],
    "buckets": [
        {
            "slug": "compromise_inside_the_perimeter",
            "description": "a host behaving like it is not ours",
            "disposition": "escalates",
            "qualifier": "always",
            "promotes_above": None,
            "promotes_into": None,
            "promotion_window_minutes": None,
            "requires_facts": ["destination_is_new_for_this_device"],
            "revision": 3,
            "updated_at": "2026-10-26 20:00:00",
            "updated_by": "console",
            "events": 400,
            "last_event": "2026-10-20T04:00:00Z",
            "unreachable": True,
            "missing_facts": ["destination_is_new_for_this_device"],
            "promotes": False,
        }
    ],
}


class StubCatalogueSensor:
    """A sensor whose bucket catalogue lives in a dict."""

    def __init__(self, *, status: int = 200, payload: dict | None = None):
        self.status = status
        self.payload = _CATALOGUE if payload is None else payload
        self.saves: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json as _json

        if request.headers.get("Authorization") != "Bearer sensor-token":
            return httpx.Response(401, json={"ok": False, "error": "bearer token required"})
        if request.url.path == "/buckets":
            if self.status != 200:
                return httpx.Response(self.status, json={"ok": False, "error": "no"})
            return httpx.Response(200, json=self.payload)
        if request.url.path == "/buckets/save":
            body = _json.loads(request.content or b"{}")
            self.saves.append(body)
            if body.get("revision") != 3:
                return httpx.Response(
                    409,
                    json={
                        "ok": False,
                        "error": "changed by somebody else",
                        "current_revision": 4,
                    },
                )
            bucket = dict(self.payload["buckets"][0])
            bucket.update({k: v for k, v in body.items() if k not in ("slug", "revision")})
            bucket["revision"] = 4
            return httpx.Response(200, json={"ok": True, "bucket": bucket, "saved": True})
        return httpx.Response(404, json={"ok": False, "error": "no route"})


def _install(monkeypatch, stub, *, url: str | None = SENSOR, token: str | None = "sensor-token"):
    if url is None:
        monkeypatch.delenv("SENTINEL_REVIEW_URL", raising=False)
    else:
        monkeypatch.setenv("SENTINEL_REVIEW_URL", url)
    if token is None:
        monkeypatch.delenv("SENTINEL_REVIEW_TOKEN", raising=False)
    else:
        monkeypatch.setenv("SENTINEL_REVIEW_TOKEN", token)
    transport = httpx.MockTransport(stub.handler)
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(sentinel_review.httpx, "AsyncClient", patched)
    return stub


def test_the_catalogue_read_carries_the_sensors_rows_and_its_build(tmp_path, monkeypatch):
    stub = _install(monkeypatch, StubCatalogueSensor())
    client = _authed_client(tmp_path, monkeypatch)

    body = client.get("/api/sentinel/buckets").json()

    assert body["build"] == "abc123def456"
    assert body["buckets"][0]["slug"] == "compromise_inside_the_perimeter"
    assert body["remainder"]["on_retired_classes"] == 749
    del stub


def test_a_save_carries_the_revision_to_the_sensor(tmp_path, monkeypatch):
    stub = _install(monkeypatch, StubCatalogueSensor())
    client = _authed_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/sentinel/buckets/save",
        json={
            "slug": "compromise_inside_the_perimeter",
            "revision": 3,
            "description": "a host behaving like it is not ours, dynamic DNS weighed",
        },
    )

    assert response.status_code == 200
    assert response.json()["bucket"]["revision"] == 4
    assert stub.saves[0]["revision"] == 3
    assert "dynamic DNS" in stub.saves[0]["description"]


def test_a_save_with_no_revision_never_reaches_the_sensor(tmp_path, monkeypatch):
    """One that is optional on the wire is not a check at all."""
    stub = _install(monkeypatch, StubCatalogueSensor())
    client = _authed_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/sentinel/buckets/save",
        json={"slug": "compromise_inside_the_perimeter", "description": "no revision"},
    )

    assert response.status_code == 400
    assert stub.saves == []


def test_a_stale_save_is_passed_through_as_a_conflict(tmp_path, monkeypatch):
    _install(monkeypatch, StubCatalogueSensor())
    client = _authed_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/sentinel/buckets/save",
        json={
            "slug": "compromise_inside_the_perimeter",
            "revision": 1,
            "description": "the stale tab's copy",
        },
    )

    assert response.status_code == 409


@pytest.mark.parametrize(
    "status, expected",
    [
        (404, sentinel_review.SENSOR_TOO_OLD),
        (503, sentinel_review.SENSOR_BUSY),
    ],
)
def test_the_page_distinguishes_why_the_catalogue_could_not_be_read(
    monkeypatch, status, expected
):
    """Five readings, five different machines to go and look at. "Sensor too
    old" is the EXPECTED first state after deploying the console half, and its
    remedy is a pull on the sensor."""
    import asyncio

    _install(monkeypatch, StubCatalogueSensor(status=status))

    state = asyncio.run(sentinel_review.catalogue_state())

    assert state["state"] == expected
    assert state["buckets"] == []


def test_an_unreachable_sensor_is_not_reported_as_an_empty_catalogue(monkeypatch):
    import asyncio

    class Dead:
        def handler(self, request):
            raise httpx.ConnectError("no route to host")

    _install(monkeypatch, Dead())

    state = asyncio.run(sentinel_review.catalogue_state())

    assert state["state"] == sentinel_review.UNREACHABLE
    assert state["buckets"] == []


def test_a_console_with_no_sensor_configured_says_so_rather_than_unreachable(monkeypatch):
    import asyncio

    _install(monkeypatch, StubCatalogueSensor(), url=None)

    state = asyncio.run(sentinel_review.catalogue_state())

    assert state["state"] == sentinel_review.NOT_CONFIGURED


def test_a_refused_bearer_token_is_not_reported_as_an_unreachable_sensor(monkeypatch):
    import asyncio

    _install(monkeypatch, StubCatalogueSensor(), token="the-wrong-token")

    state = asyncio.run(sentinel_review.catalogue_state())

    assert state["state"] == sentinel_review.CREDENTIAL_REFUSED


def test_a_sensor_holding_no_catalogue_reads_as_reachable_with_nothing(monkeypatch):
    import asyncio

    _install(
        monkeypatch,
        StubCatalogueSensor(payload={"ok": True, "build": "abc", "buckets": []}),
    )

    state = asyncio.run(sentinel_review.catalogue_state())

    assert state["state"] == sentinel_review.EMPTY
