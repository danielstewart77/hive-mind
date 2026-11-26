"""A user's key changes the mind that user owns and no other (feature 834, requirement 19).

The mind's real `authorize_admin` asks a real gateway (the comms application,
served on a local port over the scratch Postgres) whether a presented key is
its owner's. Protected behaviour 2, widened: a key reaches only what it should.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import socket
import sys
import threading
import time

import pytest
import uvicorn
from starlette.requests import Request

NS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "nervous-system"))
if NS not in sys.path:
    sys.path.insert(0, NS)

from comms import user_access  # noqa: E402
from comms_broker_pg import (  # noqa: E402,F401 - fixtures
    broker_db, put_credential_row, put_mind, scratch_dsn,
)

from minds import runtime_api  # noqa: E402

ALPHA, BETA = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
ADMIN = "admin-bearer"  # secret-guard: allow
ALPHA_KEY = "alpha-mind-key-for-tests"  # secret-guard: allow
BETA_KEY = "beta-mind-key-for-tests"  # secret-guard: allow
DANIEL_KEY = "daniel-person-key-for-tests"  # secret-guard: allow
SLOAN_KEY = "sloan-person-key-for-tests"  # secret-guard: allow


def _request(bearer: str | None) -> Request:
    headers = [(b"authorization", f"Bearer {bearer}".encode())] if bearer is not None else []
    return Request({"type": "http", "headers": headers, "path": "/runtime"})


async def _seed() -> None:
    await put_mind(ALPHA, "alpha", owner="daniel")
    await put_mind(BETA, "beta", owner="sloan")
    for holder, user, key in (("alpha", "daniel", ALPHA_KEY), ("beta", "sloan", BETA_KEY)):
        await put_credential_row(holder, user, user_access.MIND, user_access.digest(key))
    for user, key in (("daniel", DANIEL_KEY), ("sloan", SLOAN_KEY)):
        await put_credential_row(user, user, user_access.USER_KEY, user_access.digest(key))


@pytest.fixture
def gateway(monkeypatch, broker_db):
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", ADMIN)
    from comms import server

    importlib.reload(server)
    asyncio.run(_seed())
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    live = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=live.run, daemon=True)
    thread.start()
    for _ in range(100):
        if live.started:
            break
        time.sleep(0.1)
    assert live.started
    monkeypatch.setenv("COMMS_URL", f"http://127.0.0.1:{port}")
    try:
        yield
    finally:
        live.should_exit = True
        thread.join(timeout=10)


def _admitted(mind_key: str, presented: str | None) -> int | None:
    """The status the real guard gives a mind holding `mind_key`; None is admitted."""
    os.environ["MIND_KEY"] = mind_key
    try:
        denied = asyncio.run(runtime_api.authorize_admin(_request(presented)))
    finally:
        del os.environ["MIND_KEY"]
    return None if denied is None else denied.status_code


def test_a_users_key_is_accepted_by_the_mind_that_user_owns_and_refused_by_another_users(gateway) -> None:
    assert _admitted(ALPHA_KEY, DANIEL_KEY) is None
    assert _admitted(ALPHA_KEY, SLOAN_KEY) == 401
    assert _admitted(BETA_KEY, SLOAN_KEY) is None
    assert _admitted(BETA_KEY, DANIEL_KEY) == 401


def test_a_mind_still_accepts_its_own_key_and_refuses_another_minds_and_none(gateway) -> None:
    assert _admitted(ALPHA_KEY, ALPHA_KEY) is None
    assert _admitted(ALPHA_KEY, BETA_KEY) == 401
    assert _admitted(ALPHA_KEY, ADMIN) == 401
    assert _admitted(ALPHA_KEY, None) == 401


def test_a_gateway_that_cannot_be_asked_is_never_taken_for_a_yes_or_a_no(gateway, monkeypatch) -> None:
    monkeypatch.setenv("COMMS_URL", "http://127.0.0.1:9")
    assert _admitted(ALPHA_KEY, DANIEL_KEY) == 503
    assert _admitted(ALPHA_KEY, ALPHA_KEY) is None
