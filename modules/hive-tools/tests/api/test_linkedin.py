"""API tests for LinkedIn endpoints."""

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


def test_post_linkedin_requires_auth(client: TestClient) -> None:
    resp = client.post("/linkedin/post", json={"content": "Hello LinkedIn"})
    assert resp.status_code == 401


@patch("tools.linkedin._load_token")
def test_post_linkedin_hitl_on_returns_202(
    mock_load: MagicMock,
    app: FastAPI,
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    mock_load.return_value = {"access_token": "tok", "expires_at": 9999999999, "user_id": "u1"}
    db.execute(
        "INSERT INTO tool_hitl_settings (tool_name, mode) VALUES (?, ?) "
        "ON CONFLICT (tool_name) DO UPDATE SET mode = excluded.mode, "
        "sudo_timeout_minutes = excluded.sudo_timeout_minutes",
        ("linkedin_post", "on"),
    )
    db.commit()

    resp = client.post(
        "/linkedin/post",
        json={"content": "Hello LinkedIn"},
        headers=auth_header,
    )
    assert resp.status_code == 202


@patch("tools.linkedin._load_token")
def test_post_linkedin_no_token_file_returns_error(
    mock_load: MagicMock,
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    mock_load.return_value = None
    resp = client.post(
        "/linkedin/post",
        json={"content": "Hello LinkedIn"},
        headers=auth_header,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data


@patch("tools.linkedin._load_token")
@patch("tools.linkedin._post_to_linkedin_impl")
def test_post_linkedin_success_returns_post_id(
    mock_post: MagicMock,
    mock_load: MagicMock,
    app: FastAPI,
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    mock_load.return_value = {"access_token": "tok", "expires_at": 9999999999, "user_id": "u1"}
    mock_post.return_value = {"ok": True, "post_id": "urn:li:share:123", "message": "Posted successfully."}
    # Disable HITL so the call goes through to the mocked implementation
    db.execute(
        "INSERT INTO tool_hitl_settings (tool_name, mode) VALUES (?, ?) "
        "ON CONFLICT (tool_name) DO UPDATE SET mode = excluded.mode, "
        "sudo_timeout_minutes = excluded.sudo_timeout_minutes",
        ("linkedin_post", "off"),
    )
    db.commit()
    resp = client.post(
        "/linkedin/post",
        json={"content": "Hello LinkedIn"},
        headers=auth_header,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["post_id"] == "urn:li:share:123"
