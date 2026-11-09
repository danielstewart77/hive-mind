"""The comms wire speaks the schema's names.

`core.sql` calls them `surface`, `surface_id` and `conversation_id`. The
routes that read them are checked against what the real session manager
stored or answered, on the scratch database.
"""

import importlib

import pytest
from fastapi.testclient import TestClient

from comms_pg_fixture import (  # noqa: F401 - fixtures
    comms_db, fetchval, scratch_dsn, seed_session,
)


GRILL_ADMIN = "admin-credential-for-tests"  # secret-guard: allow

MIND = "ada"
SESSION = "sess-1"
CONVERSATION = "conv-1"
CHAT = "chat-9"
PANE = ("terminal", "terminal-sess-1")


@pytest.fixture()
def client(monkeypatch, comms_db):  # noqa: F811 - the imported fixture
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", GRILL_ADMIN)

    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app, headers={"Authorization": f"Bearer {GRILL_ADMIN}"}) as http:
        yield http, server_module


def _seed(http, session_id: str = SESSION, **columns) -> None:
    """On the app's own loop: the pool the lifespan opened lives there."""
    columns.setdefault("surface", "telegram")
    columns.setdefault("surface_id", CHAT)
    columns.setdefault("conversation_id", CONVERSATION)
    http.portal.call(lambda: seed_session(session_id, mind_id=MIND, **columns))


# Every route that once took an old name, a request it would otherwise
# accept under the schema's names, and the old name added beside them.
_PANE_BODY = {"client_type": PANE[0], "client_ref": PANE[1], "conversation_id": CONVERSATION}
def test_an_arm_is_stored_only_for_the_conversation_it_names(client):
    """A route dropping `conversation_id` arms whatever conversation is there."""
    http, server = client
    _seed(http, bind=PANE)
    armed = "SELECT rotation_armed FROM core.sessions WHERE id = $1"

    stale = http.post("/sessions/arm-rotation", json={**_PANE_BODY, "conversation_id": "conv-0"})
    assert stale.status_code == 200, stale.text
    assert http.portal.call(fetchval, armed, SESSION) != server.sessions.ROTATION_ARMED

    current = http.post("/sessions/arm-rotation", json=_PANE_BODY)
    assert current.status_code == 200, current.text
    assert http.portal.call(fetchval, armed, SESSION) == server.sessions.ROTATION_ARMED


def test_a_turn_clears_the_carry_forward_only_of_the_conversation_it_names(client):
    """A straggling turn from the replaced conversation keeps the new seed."""
    http, _server = client
    _seed(http, bind=PANE, carry_forward="the seed", carry_forward_sid=CONVERSATION)
    stored = "SELECT carry_forward FROM core.sessions WHERE id = $1"
    turn = {**_PANE_BODY, "role": "user", "content": "hi"}

    straggler = http.post("/sessions/record-turn", json={**turn, "conversation_id": "conv-0"})
    assert straggler.status_code == 200, straggler.text
    assert http.portal.call(fetchval, stored, SESSION) == "the seed"

    landed = http.post("/sessions/record-turn", json=turn)
    assert landed.status_code == 200, landed.text
    assert http.portal.call(fetchval, stored, SESSION) is None


def test_the_carry_forward_is_answered_for_the_conversation_named(client):
    http, _server = client
    _seed(http, carry_forward="the seed", carry_forward_sid=CONVERSATION)

    response = http.get(f"/sessions/{SESSION}/carry-forward",
                        params={"conversation_id": CONVERSATION})

    assert response.status_code == 200, response.text
    assert response.json()["carry_forward"] == "the seed"


def test_listing_sessions_filters_on_the_surface_id(client):
    http, _server = client
    _seed(http)
    _seed(http, "sess-other", surface_id="chat-other", conversation_id="conv-other")

    response = http.get("/sessions", params={"surface_id": CHAT})

    assert response.status_code == 200, response.text
    assert [s["id"] for s in response.json()] == [SESSION]


def test_the_sessions_command_lists_the_surface_ids_own_conversations(client):
    http, _server = client
    _seed(http)
    _seed(http, "sess-other", surface_id="chat-other", conversation_id="conv-other")

    response = http.post("/command", json={
        "content": "/sessions", "surface": "telegram", "surface_id": CHAT,
        "client_ref": CHAT, "mind_id": MIND,
    })

    assert response.status_code == 200, response.text
    assert [s["id"] for s in response.json()] == [SESSION]
