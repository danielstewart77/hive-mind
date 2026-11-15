"""A person on a bot reaches their own mind and nobody else's (story 2.1.15).

The bot's credential says which box is calling. The person is the account
the message came from, looked up in `core.surface_accounts`, and they reach
only a mind they own. Driven through the real gateway routes on a scratch
database; the session manager's spawn is replaced only where the test is
about admission and a real spawn would need a live mind.
"""

from __future__ import annotations

import importlib
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from comms_pg_fixture import (  # noqa: F401 - fixtures
    comms_db, link_account, register_mind, scratch_dsn, seed_session,
)

ADMIN = "admin-credential-for-tests"  # secret-guard: allow

SKIPPY = "aaaaaaaa-1111-4111-8111-111111111111"
ARNOLD = "bbbbbbbb-2222-4222-8222-222222222222"
SLOAN_TG = "5550001"
DANIEL_TG = "8776938611"
STRANGER_TG = "5559999"


@pytest.fixture()
def gateway(monkeypatch, comms_db):  # noqa: F811
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", ADMIN)
    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app, headers={"Authorization": f"Bearer {ADMIN}"}) as http:
        async def seed() -> None:
            await register_mind(SKIPPY, name="skippy-t", owner="daniel")
            await register_mind(ARNOLD, name="arnold-t", owner="sloan")
            await link_account("telegram", SLOAN_TG, "sloan")
            await link_account("telegram", DANIEL_TG, "daniel")
        http.portal.call(seed)
        yield http, server_module


def _open(http, server_module, mind_id: str, sender: str):
    created = {"id": "new-session", "mind_id": mind_id}
    with patch.object(server_module.session_mgr, "create_session",
                      new=AsyncMock(return_value=created)):
        return http.post("/sessions", json={
            "surface": f"telegram:{mind_id}", "surface_id": sender,
            "client_ref": sender, "mind_id": mind_id,
        })


def test_a_second_users_linked_account_reaches_their_mind_and_not_skippy(gateway):
    http, server_module = gateway

    theirs = _open(http, server_module, ARNOLD, SLOAN_TG)
    skippy = _open(http, server_module, SKIPPY, SLOAN_TG)

    assert theirs.status_code == 200, theirs.text
    assert theirs.json()["mind_id"] == ARNOLD
    assert skippy.status_code == 404
    assert skippy.json() == {"error": "Mind not found"}


def test_an_unlinked_account_is_refused_even_on_the_operators_own_mind(gateway):
    http, server_module = gateway

    stranger = _open(http, server_module, SKIPPY, STRANGER_TG)
    daniel_on_arnold = _open(http, server_module, ARNOLD, DANIEL_TG)

    assert stranger.status_code == 404
    assert daniel_on_arnold.status_code == 404


def test_a_bot_does_not_reuse_a_conversation_its_sender_may_no_longer_reach(gateway):
    """The live case: Daniel's chat still bound to a conversation on a mind
    Aiden owns. The bot's reuse lookup must not hand it back, because the
    turn that follows carries no sender to check."""
    http, _ = gateway

    # Daniel's account is already linked to him, so the fixture's own link
    # for the seeded sender is a no-op and the binding stays foreign.
    http.portal.call(lambda: seed_session(
        "sess-stale", surface=f"telegram:{ARNOLD}", surface_id=DANIEL_TG,
        mind_id=ARNOLD, bind=(f"telegram:{ARNOLD}", DANIEL_TG)))

    found = http.get("/sessions", params={
        "client_type": f"telegram:{ARNOLD}", "client_ref": DANIEL_TG})

    assert found.status_code == 200
    assert found.json() == []


def test_a_bot_still_reuses_its_own_persons_conversation(gateway):
    http, _ = gateway
    http.portal.call(lambda: seed_session(
        "sess-own", surface=f"telegram:{ARNOLD}", surface_id=SLOAN_TG,
        mind_id=ARNOLD, bind=(f"telegram:{ARNOLD}", SLOAN_TG)))

    found = http.get("/sessions", params={
        "client_type": f"telegram:{ARNOLD}", "client_ref": SLOAN_TG})

    assert [row["id"] for row in found.json()] == ["sess-own"]


def test_a_command_from_an_account_that_is_not_the_owners_is_refused(gateway):
    http, _ = gateway

    refused = http.post("/command", json={
        "content": "/sessions", "surface": f"telegram:{SKIPPY}",
        "surface_id": SLOAN_TG, "client_ref": SLOAN_TG, "mind_id": SKIPPY,
    })
    allowed = http.post("/command", json={
        "content": "/sessions", "surface": f"telegram:{ARNOLD}",
        "surface_id": SLOAN_TG, "client_ref": SLOAN_TG, "mind_id": ARNOLD,
    })

    assert refused.json() == {"error": "Session not found"}
    assert isinstance(allowed.json(), list)
