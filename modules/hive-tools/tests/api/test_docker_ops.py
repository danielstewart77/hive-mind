"""API tests for Docker Ops endpoints."""

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


def test_docker_status_requires_auth(client: TestClient) -> None:
    resp = client.get("/docker/status?project=hive_mind")
    assert resp.status_code == 401


def test_docker_compose_up_hitl_on_returns_202(
    app: FastAPI,
    client: TestClient,
    auth_header: dict[str, str],
    tmp_path,
) -> None:
    db.execute(
        "INSERT INTO tool_hitl_settings (tool_name, mode) VALUES (?, ?) "
        "ON CONFLICT (tool_name) DO UPDATE SET mode = excluded.mode, "
        "sudo_timeout_minutes = excluded.sudo_timeout_minutes",
        ("docker_compose_up", "on"),
    )
    db.commit()

    # The path must be a real directory or _validate_path short-circuits before HITL.
    resp = client.post(
        f"/docker/up?path={tmp_path}",
        headers=auth_header,
    )
    assert resp.status_code == 202


@patch("tools.docker_ops._run_subprocess")
def test_docker_compose_logs_returns_output(
    mock_run: MagicMock,
    client: TestClient,
    auth_header: dict[str, str],
    tmp_path,
) -> None:
    mock_run.return_value = (0, "log line 1\nlog line 2", "")
    resp = client.get(f"/docker/logs?path={tmp_path}", headers=auth_header)
    assert resp.status_code == 200


def test_docker_invalid_project_returns_error(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    resp = client.get("/docker/status?path=/no/such/path/xyz", headers=auth_header)
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data


@patch("tools.docker_ops._run_subprocess")
def test_docker_list_containers_returns_array(
    mock_run: MagicMock,
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    mock_run.return_value = (0, '{"Names":"test","Image":"img","State":"running","Status":"Up","Ports":""}\n', "")
    resp = client.get("/docker/containers", headers=auth_header)
    assert resp.status_code == 200
    data = resp.json()
    assert "containers" in data


@patch("tools.docker_ops._run_subprocess")
def test_docker_list_networks_returns_array(
    mock_run: MagicMock,
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    mock_run.return_value = (0, '{"Name":"bridge","Driver":"bridge","Scope":"local"}\n', "")
    resp = client.get("/docker/networks", headers=auth_header)
    assert resp.status_code == 200
    data = resp.json()
    assert "networks" in data


def test_a_gated_call_whose_body_cannot_be_stored_is_refused(
    app: FastAPI,
    client: TestClient,
    auth_header: dict[str, str],
    tmp_path,
) -> None:
    """A NUL byte is valid UTF-8, so `errors="replace"` leaves it alone.
    SQLite stored it and Postgres refuses it outright, so the same gated call
    was queued on one store and 500ed on the other with no approval row and
    no notification. Refused here instead, because a body stored with the NUL
    taken out would replay a different call than the one approved."""
    db.execute(
        "INSERT INTO tool_hitl_settings (tool_name, mode) VALUES (?, ?) "
        "ON CONFLICT (tool_name) DO UPDATE SET mode = excluded.mode, "
        "sudo_timeout_minutes = excluded.sudo_timeout_minutes",
        ("docker_compose_up", "on"))
    db.commit()

    resp = client.post(
        f"/docker/up?path={tmp_path}",
        content=b'{"service": "a\x00b"}',
        headers={**auth_header, "Content-Type": "application/json"},
    )

    assert resp.status_code == 400
    assert "NUL" in resp.json()["error"]
    # Nothing queued, so nothing is waiting on a person for a call that
    # cannot be replayed.
    assert db.execute(
        "SELECT count(*) AS c FROM hitl_requests").fetchone()["c"] == 0


def test_a_body_with_invalid_utf8_is_still_queued(
    app: FastAPI,
    client: TestClient,
    auth_header: dict[str, str],
    tmp_path,
) -> None:
    """Only the NUL is refused. Invalid UTF-8 has always been captured with
    the replacement character and is still gated, so the refusal is as narrow
    as the thing the database cannot hold."""
    db.execute(
        "INSERT INTO tool_hitl_settings (tool_name, mode) VALUES (?, ?) "
        "ON CONFLICT (tool_name) DO UPDATE SET mode = excluded.mode, "
        "sudo_timeout_minutes = excluded.sudo_timeout_minutes",
        ("docker_compose_up", "on"))
    db.commit()

    resp = client.post(
        f"/docker/up?path={tmp_path}",
        content=b'{"service": "\xff\xfe"}',
        headers={**auth_header, "Content-Type": "application/json"},
    )

    assert resp.status_code == 202
    assert db.execute(
        "SELECT count(*) AS c FROM hitl_requests").fetchone()["c"] == 1


def test_mutating_a_blocked_project_is_refused_not_a_fault(
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    """`docker compose up` on hive-tools' own directory is the service
    restarting itself mid-request. It answered `500 Internal Server Error`,
    because the handler written for it imported a class that did not exist
    and the import sat inside `except ImportError: pass` - so the handler was
    silently never registered."""
    resp = client.post(
        "/docker/up?path=/srv/hive-tools",
        headers=auth_header,
    )

    assert resp.status_code == 403
    assert "blocked" in resp.json()["error"]
