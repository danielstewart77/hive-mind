"""API tests for the FastAPI application skeleton."""

from __future__ import annotations

from collections.abc import Generator

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
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
    with TestClient(app) as entered:
        yield entered


def test_health_endpoint_returns_200(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_starting_the_app_opens_its_store(app: FastAPI) -> None:
    """Nothing opens a database while the app is being built, so the lifespan
    is the only thing that can. An operation reached before start says so
    rather than connecting on demand behind somebody's back."""

    import store

    with pytest.raises(RuntimeError):
        run(store.count_users())

    with TestClient(app):
        # The bootstrap admin is seeded at start, so a read answering at all
        # is the store being open and a count of one is the seed having run.
        assert run(store.count_users()) == 1

    with pytest.raises(RuntimeError):
        run(store.count_users())
