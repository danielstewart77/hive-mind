"""API tests for login endpoints."""

from __future__ import annotations

from collections.abc import Generator

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth_session import create_user
from tests.scratch import run


@pytest.fixture()
def app(tmp_path: Path) -> FastAPI:
    from server import create_app

    return create_app()


@pytest.fixture()
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    # Entered, not merely constructed: the store opens in the app's
    # lifespan, and a client that never enters it is an app whose database
    # was never opened.
    with TestClient(app, follow_redirects=False) as entered:
        yield entered


def test_get_login_page_returns_html(client: TestClient) -> None:
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_post_login_valid_sets_cookie(app: FastAPI, client: TestClient) -> None:
    run(create_user("daniel", "pass123", is_admin=True))
    resp = client.post(
        "/login",
        data={"username": "daniel", "password": "pass123"},
    )
    assert resp.status_code in (302, 303)
    assert "ht_session" in resp.cookies


def test_post_login_invalid_returns_401(client: TestClient) -> None:
    resp = client.post(
        "/login",
        data={"username": "wrong", "password": "wrong"},
    )
    assert resp.status_code == 401


def test_authenticated_route_without_session_redirects(client: TestClient) -> None:
    resp = client.get("/")
    # Should redirect to login or return 401
    assert resp.status_code in (302, 303, 401)
