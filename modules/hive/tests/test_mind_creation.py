"""Around creating an internal Mind from the console: the form, and which
minds the console sends its admin credential to.

No test here creates a mind, and none is to be added. Creating one writes an
include line into the stack's compose file, token lines into the hive's env
file, a folder into the minds folder and a registration on the gateway. The
tests that did it redirected some of those and not the rest, and which was
which depended on where the suite ran: on 2026-11-17 a run inside a mind's
container registered three fixture minds on the live gateway, and inside the
console's own container it would have written the live compose and env
files. Daniel, 2026-11-17: "it's not okay to write tests, to insert
important things into live code... if its job is to insert something into
production, then we have to talk about it and see if there is a way to
safely test it." See "Code whose job is to write into production" in the
code skill's test-standard.md. Until that conversation, creation is
unprotected, on purpose.
"""

import os
import sys
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth
import configuration_data
import main as main_mod
import accounts

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    minds_dir = tmp_path / "minds"
    minds_dir.mkdir()
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_DIR", minds_dir)
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_CONFIG_DIR", minds_dir)
    monkeypatch.setattr(main_mod, "_broker_minds_safe", AsyncMock(return_value=[]))
    user = accounts.make_user("daniel", "secret-pass", is_admin=True, replace=True)
    test_client = TestClient(main_mod.app)
    test_client.cookies.set(auth.SESSION_COOKIE_NAME, accounts.session_token(user))
    test_client.minds_dir = minds_dir
    return test_client


def test_the_form_offers_only_pairs_the_api_accepts(client):
    page = client.get("/minds/new").text
    assert 'value="claude_cli"' in page
    assert 'value="codex_cli"' in page
    # Identity is deliberately not scaffolded here.
    assert "soul" in page.lower()


def test_the_console_withholds_its_admin_credential_from_minds_the_gateway_withholds_it_from(
    client, monkeypatch,
):
    """A kid's box elsewhere on the network, created nowhere near this
    console, is not to be sent it either; the gateway marks which minds are."""
    import asyncio
    import route_access
    import mind_runtime

    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-token")
    registry = [
        {"id": "arnold-id", "name": "arnold", "owner": "sloan",
         "gateway_url": "http://192.168.5.64:8433", "admin_credential_allowed": False},
        {"id": "bob-id", "name": "bob", "owner": "daniel",
         "gateway_url": "http://bob:8420", "admin_credential_allowed": True},
    ]
    monkeypatch.setattr(route_access, "registered_minds", AsyncMock(return_value=registry))

    with pytest.raises(main_mod.HTTPException) as refused:
        asyncio.run(mind_runtime.admin_headers("http://192.168.5.64:8433"))
    assert refused.value.status_code == 503
    assert asyncio.run(mind_runtime.admin_headers("http://bob:8420")) == {
        "Authorization": "Bearer admin-token"}
