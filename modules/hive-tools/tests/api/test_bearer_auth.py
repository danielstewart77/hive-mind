"""API tests for bearer token authentication."""

from __future__ import annotations

from contextlib import asynccontextmanager

from collections.abc import Generator

from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import store
from auth_api import require_api_token
from tests.scratch import generate_token
from tests.scratch import run


@pytest.fixture()
def app_with_auth(tmp_path: Path) -> FastAPI:
    """A bare app around the token dependency.

    Not `create_app`: the point here is the dependency in isolation. The
    store still has to be opened for it, and it is opened in this app's own
    lifespan for the same reason the real one is.
    """
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await store.open()
        try:
            yield
        finally:
            await store.close()

    app = FastAPI(lifespan=lifespan)

    @app.get("/protected")
    async def protected(caller: str = Depends(require_api_token)):
        return {"caller": caller}

    return app


@pytest.fixture()
def client(app_with_auth: FastAPI) -> Generator[TestClient, None, None]:
    # Entered, not merely constructed: the store opens in the app's
    # lifespan, and a client that never enters it is an app whose database
    # was never opened.
    with TestClient(app_with_auth) as entered:
        yield entered


def test_endpoint_without_auth_returns_401(client: TestClient) -> None:
    resp = client.get("/protected")
    assert resp.status_code == 401


def test_endpoint_with_invalid_token_returns_401(client: TestClient) -> None:
    resp = client.get("/protected", headers={"Authorization": "Bearer bad-token"})
    assert resp.status_code == 401


def test_endpoint_with_valid_token_returns_200(client: TestClient) -> None:
    raw = run(generate_token("ada"))

    resp = client.get("/protected", headers={"Authorization": f"Bearer {raw}"})
    assert resp.status_code == 200
    assert resp.json()["caller"] == "ada"


# ---------------------------------------------------------------------------
# DECIDED 2026-11-15 (Daniel): his integrations are usable by his minds only.
# Until a user connects their own (story 2.1.16), a call from a mind whose
# owner is not the person whose accounts these are is refused outright, never
# routed to him for approval.
# ---------------------------------------------------------------------------
def _a_mind(name: str, owner: str) -> str:
    import time as _time

    from hive_data import core

    async def seed() -> None:
        # The store is the client's, opened in the app's lifespan.
        now = _time.time()
        await core.upsert_mind(
            mind_id=f"{name}-id", name=name, gateway_url=f"http://{name}:8420",
            model="sonnet", harness="claude_cli", registered_at=now,
            last_seen=now, owner=owner)
        if await store.user_by_username("daniel") is None:
            await store.create_user("daniel", "x", 1, int(now))

    run(seed())
    return run(generate_token(name))


def test_a_mind_of_someone_else_is_refused_however_valid_its_token(client: TestClient) -> None:
    raw = _a_mind("arnold", owner="sloan")

    resp = client.get("/protected", headers={"Authorization": f"Bearer {raw}"})

    assert resp.status_code == 403
    assert "sloan" in resp.json()["detail"]


def test_a_mind_of_the_person_whose_accounts_these_are_is_let_through(client: TestClient) -> None:
    raw = _a_mind("bob", owner="Daniel")

    resp = client.get("/protected", headers={"Authorization": f"Bearer {raw}"})

    assert (resp.status_code, resp.json()) == (200, {"caller": "bob"})


def test_a_mind_registered_under_another_case_or_spacing_is_still_its_owners(
    client: TestClient,
) -> None:
    """comms lets a mind re-register as "Arnold" or " arnold"; it is still
    the mind its token was minted for."""
    _a_mind(" Arnold", owner="sloan")
    raw = run(generate_token("arnold"))

    resp = client.get("/protected", headers={"Authorization": f"Bearer {raw}"})

    assert resp.status_code == 403


def test_a_disabled_admins_minds_are_not_let_through(client: TestClient) -> None:
    import time as _time

    from hive_data import tools as _tools

    raw = _a_mind("bob", owner="daniel")
    run(_tools.disable_user("daniel", int(_time.time())))

    resp = client.get("/protected", headers={"Authorization": f"Bearer {raw}"})

    assert resp.status_code == 403
