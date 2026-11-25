"""API tests for HITL endpoints."""

from __future__ import annotations

from collections.abc import Generator

import time
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth_api import generate_approver_token, generate_token
from tests.scratch import db, run


def _make_app(tmp_path: Path) -> FastAPI:
    """Create an app with HITL endpoints wired."""
    from server import create_app

    app = create_app()
    return app


@pytest.fixture()
def app(tmp_path: Path) -> FastAPI:
    return _make_app(tmp_path)


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


@pytest.fixture()
def approver_header(client: TestClient) -> dict[str, str]:
    raw = run(generate_approver_token())
    return {"Authorization": f"Bearer {raw}"}


def _insert_hitl_request(app: FastAPI, status: str = "pending", expired: bool = False) -> str:
    request_id = str(uuid.uuid4())
    now = int(time.time())
    expires_at = now - 10 if expired else now + 300
    decided_at = now if status != "pending" else None
    db.execute(
        "INSERT INTO hitl_requests (id, tool_name, caller_mind, params_json, status, created_at, expires_at, decided_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (request_id, "test_tool", "ada", "{}", status, now, expires_at, decided_at),
    )
    db.commit()
    return request_id


def test_get_hitl_status_pending_returns_202(
    app: FastAPI, client: TestClient, auth_header: dict[str, str]
) -> None:
    request_id = _insert_hitl_request(app, "pending")
    resp = client.get(f"/hitl/{request_id}", headers=auth_header)
    assert resp.status_code == 202


def test_get_hitl_status_approved_returns_200(
    app: FastAPI, client: TestClient, auth_header: dict[str, str]
) -> None:
    request_id = _insert_hitl_request(app, "approved")
    resp = client.get(f"/hitl/{request_id}", headers=auth_header)
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"


def test_get_hitl_status_denied_returns_403(
    app: FastAPI, client: TestClient, auth_header: dict[str, str]
) -> None:
    request_id = _insert_hitl_request(app, "denied")
    resp = client.get(f"/hitl/{request_id}", headers=auth_header)
    assert resp.status_code == 403


def test_get_hitl_status_expired_returns_408(
    app: FastAPI, client: TestClient, auth_header: dict[str, str]
) -> None:
    request_id = _insert_hitl_request(app, "pending", expired=True)
    resp = client.get(f"/hitl/{request_id}", headers=auth_header)
    assert resp.status_code == 408


def test_get_hitl_status_unknown_id_returns_404(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    resp = client.get(f"/hitl/{uuid.uuid4()}", headers=auth_header)
    assert resp.status_code == 404


def test_post_hitl_respond_approve_updates_status(
    app: FastAPI, client: TestClient, approver_header: dict[str, str]
) -> None:
    request_id = _insert_hitl_request(app, "pending")
    resp = client.post(
        f"/hitl/{request_id}/respond",
        json={"action": "approve"},
        headers=approver_header,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"


def test_post_hitl_respond_deny_updates_status(
    app: FastAPI, client: TestClient, approver_header: dict[str, str]
) -> None:
    request_id = _insert_hitl_request(app, "pending")
    resp = client.post(
        f"/hitl/{request_id}/respond",
        json={"action": "deny"},
        headers=approver_header,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "denied"


def test_post_hitl_respond_approve_sudo_creates_grant(
    app: FastAPI, client: TestClient, approver_header: dict[str, str]
) -> None:
    # Set tool to sudo mode
    db.execute(
        "INSERT INTO tool_hitl_settings (tool_name, mode, sudo_timeout_minutes) VALUES (?, ?, ?)",
        ("test_tool", "sudo", 15),
    )
    db.commit()

    request_id = _insert_hitl_request(app, "pending")
    resp = client.post(
        f"/hitl/{request_id}/respond",
        json={"action": "approve"},
        headers=approver_header,
    )
    assert resp.status_code == 200

    grant = db.execute(
        "SELECT * FROM sudo_grants WHERE tool_name = ? AND caller_mind = ?",
        ("test_tool", "ada"),
    ).fetchone()
    assert grant is not None


def test_post_hitl_respond_already_resolved_returns_409(
    app: FastAPI, client: TestClient, approver_header: dict[str, str]
) -> None:
    request_id = _insert_hitl_request(app, "approved")
    resp = client.post(
        f"/hitl/{request_id}/respond",
        json={"action": "approve"},
        headers=approver_header,
    )
    assert resp.status_code == 409
