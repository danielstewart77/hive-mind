"""API tests for the SMS send endpoint."""

from __future__ import annotations

from collections.abc import Generator

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth_api import generate_token
from tests.scratch import db, run


@pytest.fixture()
def app(tmp_path: Path) -> FastAPI:
    from server import create_app

    return create_app()


@pytest.fixture()
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    # Entered, not merely constructed: the store opens in the app's
    # lifespan, and a client that never enters it is an app whose database
    # was never opened.
    with TestClient(app, raise_server_exceptions=False) as entered:
        yield entered


@pytest.fixture()
def auth_header(client: TestClient) -> dict[str, str]:
    raw = run(generate_token("ada"))
    return {"Authorization": f"Bearer {raw}"}


def _gateway_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMS_GATEWAY_URL", "http://sms-gateway.test:8080")
    monkeypatch.setenv("SMS_GATEWAY_USER", "user")
    monkeypatch.setenv("SMS_GATEWAY_PASS", "pass")


def _hitl_off(app: FastAPI) -> None:
    db.execute(
        "INSERT INTO tool_hitl_settings (tool_name, mode) VALUES (?, ?) "
        "ON CONFLICT (tool_name) DO UPDATE SET mode = excluded.mode, "
        "sudo_timeout_minutes = excluded.sudo_timeout_minutes",
        ("sms_send", "off"),
    )
    db.commit()


def test_send_requires_auth(client: TestClient) -> None:
    resp = client.post("/sms/send", json={"to": "+15551234567", "body": "hi"})
    assert resp.status_code == 401


def test_send_hitl_on_returns_202(
    app: FastAPI,
    client: TestClient,
    auth_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _gateway_env(monkeypatch)
    db.execute(
        "INSERT INTO tool_hitl_settings (tool_name, mode) VALUES (?, ?) "
        "ON CONFLICT (tool_name) DO UPDATE SET mode = excluded.mode, "
        "sudo_timeout_minutes = excluded.sudo_timeout_minutes",
        ("sms_send", "on"),
    )
    db.commit()

    resp = client.post(
        "/sms/send",
        json={"to": "+15551234567", "body": "hi"},
        headers=auth_header,
    )
    assert resp.status_code == 202


def test_send_returns_error_when_gateway_unconfigured(
    app: FastAPI,
    client: TestClient,
    auth_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SMS_GATEWAY_URL", raising=False)
    monkeypatch.delenv("SMS_GATEWAY_USER", raising=False)
    monkeypatch.delenv("SMS_GATEWAY_PASS", raising=False)
    _hitl_off(app)

    resp = client.post(
        "/sms/send",
        json={"to": "+15551234567", "body": "hi"},
        headers=auth_header,
    )
    assert resp.status_code == 200
    assert "not configured" in resp.json()["error"]


def test_send_posts_to_gateway_and_returns_payload(
    app: FastAPI,
    client: TestClient,
    auth_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _gateway_env(monkeypatch)
    _hitl_off(app)

    gateway_response = MagicMock()
    gateway_response.status_code = 202
    gateway_response.json.return_value = {"id": "gw-123", "state": "Pending"}
    gateway_response.text = '{"id":"gw-123"}'

    mock_client = AsyncMock()
    mock_client.post.return_value = gateway_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch("tools.sms.httpx.AsyncClient", return_value=mock_client):
        resp = client.post(
            "/sms/send",
            json={"to": "+15551234567", "body": "hello there"},
            headers=auth_header,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "sent"
    assert data["to"] == "+15551234567"
    assert data["gateway"]["id"] == "gw-123"

    args, kwargs = mock_client.post.call_args
    assert args[0].endswith("/api/3rdparty/v1/messages")
    assert kwargs["json"] == {"message": "hello there", "phoneNumbers": ["+15551234567"]}
    assert kwargs["auth"] == ("user", "pass")


def test_send_surfaces_gateway_error_status(
    app: FastAPI,
    client: TestClient,
    auth_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _gateway_env(monkeypatch)
    _hitl_off(app)

    gateway_response = MagicMock()
    gateway_response.status_code = 401
    gateway_response.text = "unauthorized"

    mock_client = AsyncMock()
    mock_client.post.return_value = gateway_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch("tools.sms.httpx.AsyncClient", return_value=mock_client):
        resp = client.post(
            "/sms/send",
            json={"to": "+15551234567", "body": "hi"},
            headers=auth_header,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "gateway returned 401" in data["error"]
    assert data["detail"] == "unauthorized"


# --- /sms/inbound/enabled proxy --------------------------------------------


def _comms_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMMS_URL", "http://hive-comms:8424")
    monkeypatch.setenv("COMMS_BEARER_TOKEN", "test-token")


def test_inbound_enabled_get_proxies_to_comms(
    client: TestClient,
    auth_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _comms_env(monkeypatch)

    comms_response = MagicMock()
    comms_response.status_code = 200
    comms_response.json.return_value = {"enabled": False}

    mock_client = AsyncMock()
    mock_client.get.return_value = comms_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch("tools.sms.httpx.AsyncClient", return_value=mock_client):
        resp = client.get("/sms/inbound/enabled", headers=auth_header)

    assert resp.status_code == 200
    assert resp.json() == {"enabled": False}

    args, kwargs = mock_client.get.call_args
    assert args[0].endswith("/sms/inbound/enabled")
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"


def test_inbound_enabled_put_proxies_to_comms(
    client: TestClient,
    auth_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _comms_env(monkeypatch)

    comms_response = MagicMock()
    comms_response.status_code = 200
    comms_response.json.return_value = {"enabled": True}

    mock_client = AsyncMock()
    mock_client.put.return_value = comms_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch("tools.sms.httpx.AsyncClient", return_value=mock_client):
        resp = client.put(
            "/sms/inbound/enabled",
            json={"enabled": True},
            headers=auth_header,
        )

    assert resp.status_code == 200
    assert resp.json() == {"enabled": True}

    args, kwargs = mock_client.put.call_args
    assert args[0].endswith("/sms/inbound/enabled")
    assert kwargs["json"] == {"enabled": True}


def test_inbound_enabled_get_requires_comms_url(
    client: TestClient,
    auth_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COMMS_URL", raising=False)
    resp = client.get("/sms/inbound/enabled", headers=auth_header)
    assert resp.status_code == 503


def test_inbound_enabled_get_surfaces_comms_error(
    client: TestClient,
    auth_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _comms_env(monkeypatch)

    comms_response = MagicMock()
    comms_response.status_code = 401
    comms_response.text = "bad token"

    mock_client = AsyncMock()
    mock_client.get.return_value = comms_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch("tools.sms.httpx.AsyncClient", return_value=mock_client):
        resp = client.get("/sms/inbound/enabled", headers=auth_header)

    assert resp.status_code == 401
