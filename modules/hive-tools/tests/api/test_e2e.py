"""End-to-end integration tests for full HITL flows."""

from __future__ import annotations

from collections.abc import Generator

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth_api import generate_approver_token, generate_token
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


def _mock_calendar_service() -> MagicMock:
    service = MagicMock()
    service.events().list().execute.return_value = {
        "items": [
            {
                "id": "evt1",
                "summary": "Test Event",
                "start": {"dateTime": "2026-01-01T10:00:00-06:00"},
                "end": {"dateTime": "2026-01-01T11:00:00-06:00"},
            }
        ]
    }
    return service


@patch("tools.calendar._get_calendar_service")
def test_full_hitl_flow_off_mode(
    mock_svc: MagicMock,
    app: FastAPI,
    client: TestClient,
) -> None:
    """Create token, call calendar endpoint (off mode), assert 200."""
    mock_svc.return_value = _mock_calendar_service()
    raw = run(generate_token("ada"))
    headers = {"Authorization": f"Bearer {raw}"}

    # calendar_list_events defaults to off
    resp = client.get("/calendar/events", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "events" in data


def test_full_hitl_flow_on_mode(
    app: FastAPI,
    client: TestClient,
) -> None:
    """Create token, call gmail send (on mode), assert 202, approve, poll, assert 200."""
    raw = run(generate_token("ada"))
    headers = {"Authorization": f"Bearer {raw}"}

    # gmail_send_email defaults to on
    resp = client.post(
        "/gmail/send",
        json={"to": "test@example.com", "subject": "hi", "body": "hello"},
        headers=headers,
    )
    assert resp.status_code == 202
    detail = resp.json()
    request_id = detail["hitl_request_id"]

    # Poll -- should be pending (202)
    resp = client.get(f"/hitl/{request_id}", headers=headers)
    assert resp.status_code == 202

    # Approve
    resp = client.post(
        f"/hitl/{request_id}/respond",
        json={"action": "approve"},
        headers={"Authorization": f"Bearer {run(generate_approver_token())}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"

    # Poll again -- should be approved (200)
    resp = client.get(f"/hitl/{request_id}", headers=headers)
    assert resp.status_code == 200


def test_full_hitl_flow_deny(
    app: FastAPI,
    client: TestClient,
) -> None:
    """Create token, call gmail send, assert 202, deny, poll, assert 403."""
    raw = run(generate_token("ada"))
    headers = {"Authorization": f"Bearer {raw}"}

    resp = client.post(
        "/gmail/send",
        json={"to": "test@example.com", "subject": "hi", "body": "hello"},
        headers=headers,
    )
    assert resp.status_code == 202
    request_id = resp.json()["hitl_request_id"]

    # Deny
    resp = client.post(
        f"/hitl/{request_id}/respond",
        json={"action": "deny"},
        headers={"Authorization": f"Bearer {run(generate_approver_token())}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "denied"

    # Poll -- should be denied (403)
    resp = client.get(f"/hitl/{request_id}", headers=headers)
    assert resp.status_code == 403


def test_full_hitl_flow_timeout(
    app: FastAPI,
    client: TestClient,
) -> None:
    """Create token, call gmail send, assert 202, manually expire, poll, assert 408."""
    raw = run(generate_token("ada"))
    headers = {"Authorization": f"Bearer {raw}"}

    resp = client.post(
        "/gmail/send",
        json={"to": "test@example.com", "subject": "hi", "body": "hello"},
        headers=headers,
    )
    assert resp.status_code == 202
    request_id = resp.json()["hitl_request_id"]

    # Manually set expires_at to the past
    now = int(time.time())
    db.execute(
        "UPDATE hitl_requests SET expires_at = ? WHERE id = ?",
        (now - 10, request_id),
    )
    db.commit()

    # Poll -- should be expired (408)
    resp = client.get(f"/hitl/{request_id}", headers=headers)
    assert resp.status_code == 408


def test_full_hitl_flow_sudo_mode(
    app: FastAPI,
    client: TestClient,
) -> None:
    """Create token, call tool in sudo mode, approve, verify grant, call again."""
    raw = run(generate_token("ada"))
    headers = {"Authorization": f"Bearer {raw}"}

    # Set docker_compose_logs to sudo mode for testing
    db.execute(
        "INSERT INTO tool_hitl_settings (tool_name, mode, sudo_timeout_minutes) VALUES (?, ?, ?) "
        "ON CONFLICT (tool_name) DO UPDATE SET mode = excluded.mode, "
        "sudo_timeout_minutes = excluded.sudo_timeout_minutes",
        ("docker_compose_logs", "sudo", 15),
    )
    db.commit()

    # First call -- should get 202
    resp = client.get("/docker/logs?project=hive_mind", headers=headers)
    assert resp.status_code == 202
    request_id = resp.json()["hitl_request_id"]

    # Approve
    resp = client.post(
        f"/hitl/{request_id}/respond",
        json={"action": "approve"},
        headers={"Authorization": f"Bearer {run(generate_approver_token())}"},
    )
    assert resp.status_code == 200

    # Check sudo grant was created
    grant = db.execute(
        "SELECT * FROM sudo_grants WHERE tool_name = ? AND caller_mind = ?",
        ("docker_compose_logs", "ada"),
    ).fetchone()
    assert grant is not None

    # Second call -- should pass through (sudo grant valid)
    resp = client.get("/docker/logs?path=/no/such/path/xyz", headers=headers)
    # Will likely fail because the path doesn't exist, but auth/HITL should pass
    assert resp.status_code == 200


def test_all_tool_routes_registered(app: FastAPI) -> None:
    """Assert all expected paths exist in app.routes."""
    paths = {getattr(r, "path", "") for r in app.routes}

    expected_paths = [
        "/health",
        "/login",
        "/hitl/{request_id}",
        "/hitl/{request_id}/respond",
        "/calendar/events",
        "/calendar/events/{event_id}",
        "/calendar/list",
        "/gmail/messages",
        "/gmail/messages/{message_id}",
        "/gmail/labels",
        "/gmail/send",
        "/gmail/reply",
        "/gmail/trash",
        "/linkedin/post",
        "/docker/up",
        "/docker/restart",
        "/docker/down",
        "/docker/logs",
        "/docker/status",
        "/docker/containers",
        "/docker/networks",
        "/tools",
        "/tokens",
    ]

    for path in expected_paths:
        assert path in paths, f"Route {path} not registered"
