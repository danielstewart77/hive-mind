"""Mind-scoped rotation settings relayed by the authenticated console routes."""

from __future__ import annotations

import json
import os
import sys

import httpx
import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from hive_site.testing import run

import auth
import main as main_mod
import mind_runtime

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _authed_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = run(auth.create_user("rotation-admin", "secret-pass", is_admin=True, replace=True))
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, run(auth.create_session_token(user)))
    return client


def _broker_rows(monkeypatch):
    async def fetch(path, *args, **kwargs):
        assert path == "/broker/minds"
        return [
            {"name": "alice", "gateway_url": "http://alice:8420"},
            {"name": "bob", "gateway_url": "http://bob:8420"},
        ]

    monkeypatch.setattr(main_mod, "_gateway_json", fetch)


def _settings_body(percent: int, window: int | None):
    return {
        "settings": [{
            "key": "rotation_threshold_percent",
            "label": "Rotation threshold",
            "kind": "number",
            "minimum": 1,
            "maximum": 100,
            "present": True,
            "value": percent,
        }],
        "rotation_threshold_percent": percent,
        "rotation_policy_error": None,
        "context_window": window,
        "rotation_threshold_tokens": (
            window * percent // 100 if window is not None else None
        ),
        "rotation_threshold_status": "known" if window is not None else "unavailable",
    }


def _use_mind_transport(monkeypatch, handler):
    def client_factory(*args, **kwargs):
        return _REAL_ASYNC_CLIENT(
            *args, transport=httpx.MockTransport(handler), **kwargs
        )

    monkeypatch.setattr(mind_runtime.httpx, "AsyncClient", client_factory)


def test_console_percentage_write_targets_only_requested_mind(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "rotation-admin-token")
    _broker_rows(monkeypatch)
    requests = []

    async def mind_service(request):
        requests.append(request)
        return httpx.Response(
            200, json=_settings_body(40, 100_000), request=request
        )

    _use_mind_transport(monkeypatch, mind_service)
    client = _authed_client(tmp_path, monkeypatch)

    response = client.patch(
        "/api/minds/bob/settings",
        json={"settings": {"rotation_threshold_percent": 40}},
    )

    assert response.status_code == 200
    assert response.json()["saved"] is True
    assert response.json()["rotation_threshold_percent"] == 40
    assert response.json()["context_window"] == 100_000
    assert len(requests) == 1
    request = requests[0]
    assert (request.method, request.url.host, request.url.path) == (
        "PATCH", "bob", "/runtime/settings"
    )
    assert request.headers["authorization"] == "Bearer rotation-admin-token"
    assert json.loads(request.content) == {
        "settings": {"rotation_threshold_percent": 40}
    }


def test_console_rotation_write_preserves_the_minds_validation_refusal(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "rotation-admin-token")
    _broker_rows(monkeypatch)
    requests = []

    async def mind_service(request):
        requests.append(request)
        return httpx.Response(
            400,
            json={"error": "Rotation threshold must be a whole number from 1 to 100"},
            request=request,
        )

    _use_mind_transport(monkeypatch, mind_service)
    client = _authed_client(tmp_path, monkeypatch)

    response = client.patch(
        "/api/minds/bob/settings",
        json={"settings": {"rotation_threshold_percent": 0}},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Rotation threshold must be a whole number from 1 to 100"
    )
    assert len(requests) == 1
    assert requests[0].url.host == "bob"


@pytest.mark.parametrize(
    ("window", "threshold", "status"),
    [(100_000, 40_000, "known"), (None, None, "unavailable")],
)
def test_console_settings_read_retains_the_saved_percent_and_capacity_state(
    tmp_path, monkeypatch, window, threshold, status
):
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "rotation-admin-token")
    _broker_rows(monkeypatch)
    requests = []

    async def mind_service(request):
        requests.append(request)
        return httpx.Response(
            200, json=_settings_body(40, window), request=request
        )

    _use_mind_transport(monkeypatch, mind_service)
    client = _authed_client(tmp_path, monkeypatch)

    response = client.get("/api/minds/bob/settings")

    assert response.status_code == 200
    body = response.json()
    assert body["rotation_threshold_percent"] == 40
    assert body["context_window"] == window
    assert body["rotation_threshold_tokens"] == threshold
    assert body["rotation_threshold_status"] == status
    assert len(requests) == 1
    assert (requests[0].method, requests[0].url.host, requests[0].url.path) == (
        "GET", "bob", "/runtime/settings"
    )


def test_rotation_settings_write_requires_the_existing_authenticated_access(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    requests = []

    async def mind_service(request):
        requests.append(request)
        return httpx.Response(200, json=_settings_body(40, 100_000), request=request)

    _use_mind_transport(monkeypatch, mind_service)
    client = TestClient(main_mod.app)

    response = client.patch(
        "/api/minds/bob/settings",
        json={"settings": {"rotation_threshold_percent": 40}},
    )

    assert response.status_code == 401
    assert requests == []
