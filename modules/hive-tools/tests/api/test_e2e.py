"""End-to-end integration tests for full HITL flows."""

from __future__ import annotations

from collections.abc import Generator

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.scratch import generate_token
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


def test_all_tool_routes_registered(app: FastAPI) -> None:
    """Assert all expected paths exist in app.routes."""
    paths = {getattr(r, "path", "") for r in app.routes}

    expected_paths = [
        "/health",
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
    ]

    for path in expected_paths:
        assert path in paths, f"Route {path} not registered"
