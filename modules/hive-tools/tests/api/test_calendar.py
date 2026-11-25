"""API tests for calendar endpoints."""

from __future__ import annotations

from collections.abc import Generator

from pathlib import Path
from unittest.mock import MagicMock, patch

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


def _mock_calendar_service():
    """Return a mock Google Calendar service."""
    service = MagicMock()
    service.events().list().execute.return_value = {
        "items": [
            {
                "id": "evt1",
                "summary": "Test Event",
                "start": {"dateTime": "2026-01-01T10:00:00-06:00"},
                "end": {"dateTime": "2026-01-01T11:00:00-06:00"},
                "status": "confirmed",
            }
        ]
    }
    service.events().get().execute.return_value = {
        "id": "evt1",
        "summary": "Test Event",
        "start": {"dateTime": "2026-01-01T10:00:00-06:00"},
        "end": {"dateTime": "2026-01-01T11:00:00-06:00"},
    }
    service.calendarList().list().execute.return_value = {
        "items": [
            {"id": "primary", "summary": "My Calendar", "timeZone": "America/Chicago", "accessRole": "owner", "primary": True}
        ]
    }
    return service


def test_list_events_requires_auth(client: TestClient) -> None:
    resp = client.get("/calendar/events")
    assert resp.status_code == 401


@patch("tools.calendar._get_calendar_service")
def test_list_events_with_auth_calls_service(
    mock_svc: MagicMock,
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    mock_svc.return_value = _mock_calendar_service()
    resp = client.get("/calendar/events", headers=auth_header)
    assert resp.status_code == 200
    data = resp.json()
    assert "events" in data


@patch("tools.calendar._get_calendar_service")
def test_list_events_hitl_mode_off_passes_through(
    mock_svc: MagicMock,
    app: FastAPI,
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    mock_svc.return_value = _mock_calendar_service()
    # Default is off, so should pass through
    resp = client.get("/calendar/events", headers=auth_header)
    assert resp.status_code == 200


@patch("tools.calendar._get_calendar_service")
def test_get_event_returns_single_event(
    mock_svc: MagicMock,
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    mock_svc.return_value = _mock_calendar_service()
    resp = client.get("/calendar/events/evt1", headers=auth_header)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "evt1"


@patch("tools.calendar._get_calendar_service")
def test_list_calendars_returns_array(
    mock_svc: MagicMock,
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    mock_svc.return_value = _mock_calendar_service()
    resp = client.get("/calendar/list", headers=auth_header)
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


def test_create_event_hitl_on_returns_202(
    app: FastAPI,
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    db.execute(
        "INSERT INTO tool_hitl_settings (tool_name, mode) VALUES (?, ?) "
        "ON CONFLICT (tool_name) DO UPDATE SET mode = excluded.mode, "
        "sudo_timeout_minutes = excluded.sudo_timeout_minutes",
        ("calendar_create_event", "on"),
    )
    db.commit()

    resp = client.post(
        "/calendar/events",
        json={
            "summary": "Test",
            "start_time": "2026-01-01T10:00:00",
            "end_time": "2026-01-01T11:00:00",
        },
        headers=auth_header,
    )
    assert resp.status_code == 202


@patch("tools.calendar._get_calendar_service")
def test_calendar_service_not_authenticated_returns_error(
    mock_svc: MagicMock,
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    mock_svc.return_value = None
    resp = client.get("/calendar/events", headers=auth_header)
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data
