"""The comms wire takes the schema's names, and the old ones for one deploy.

`core.sql` calls them `surface`, `surface_id` and `conversation_id`; the wire
called them `owner_type`, `owner_ref` and `claude_sid`. Consumers move to the
schema's names one at a time — the edge minds deploy on their own hosts — so
for one deploy comms honours both. A request under either name must reach the
session manager with the value it carried, not with a default.
"""

import importlib
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from comms_pg_fixture import comms_db, scratch_dsn  # noqa: F401 - fixtures


GRILL_ADMIN = "admin-credential-for-tests"  # secret-guard: allow

SCHEMA = {"surface": "surface", "surface_id": "surface_id",
          "conversation_id": "conversation_id"}
OLD = {"surface": "owner_type", "surface_id": "owner_ref",
       "conversation_id": "claude_sid"}
NAMES = pytest.mark.parametrize("names", [SCHEMA, OLD], ids=["schema-names", "old-names"])


@pytest.fixture()
def client(monkeypatch, comms_db):  # noqa: F811 - the imported fixture
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", GRILL_ADMIN)

    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app, headers={"Authorization": f"Bearer {GRILL_ADMIN}"}) as http:
        yield http, server_module


@NAMES
def test_creating_a_session_takes_the_surface_under_either_name(client, names):
    http, server = client
    create = AsyncMock(return_value={"id": "sess-1"})

    with patch.object(server.session_mgr, "create_session", create), \
            patch.object(server, "_names_own_mind", AsyncMock(return_value=True)):
        response = http.post("/sessions", json={
            names["surface"]: "telegram:mind-1",
            names["surface_id"]: "chat-9",
            "client_ref": "chat-9",
            "mind_id": "mind-1",
        })

    assert response.status_code == 200, response.text
    assert create.await_args.kwargs["owner_type"] == "telegram:mind-1"
    assert create.await_args.kwargs["owner_ref"] == "chat-9"


@NAMES
def test_arming_a_rotation_takes_the_conversation_under_either_name(client, names):
    http, server = client
    arm = AsyncMock(return_value={"ok": True})

    with patch.object(server.session_mgr, "arm_rotation", arm), \
            patch.object(server, "_refuse_foreign_client_ref", AsyncMock(return_value=None)):
        response = http.post("/sessions/arm-rotation", json={
            "client_type": "terminal", "client_ref": "terminal-sess-1",
            names["conversation_id"]: "conv-7",
        })

    assert response.status_code == 200, response.text
    assert arm.await_args.kwargs["claude_sid"] == "conv-7"


@NAMES
def test_listing_sessions_filters_on_the_surface_id_under_either_name(client, names):
    http, server = client
    listing = AsyncMock(return_value=[])

    with patch.object(server.session_mgr, "list_sessions", listing):
        response = http.get("/sessions", params={names["surface_id"]: "chat-9"})

    assert response.status_code == 200, response.text
    assert listing.await_args.kwargs["owner_ref"] == "chat-9"


@NAMES
def test_the_carry_forward_is_looked_up_by_the_conversation_under_either_name(client, names):
    http, server = client
    lookup = AsyncMock(return_value="seed")

    with patch.object(server.session_mgr, "get_carry_forward", lookup), \
            patch.object(server, "_require_own_session_mind", AsyncMock(return_value=None)):
        response = http.get(
            "/sessions/sess-1/carry-forward",
            params={names["conversation_id"]: "conv-7"},
        )

    assert response.status_code == 200, response.text
    assert lookup.await_args.args == ("sess-1", "conv-7")


@NAMES
def test_recording_a_turn_takes_the_conversation_under_either_name(client, names):
    http, server = client
    record = AsyncMock(return_value={"ok": True})

    with patch.object(server.session_mgr, "record_turn", record), \
            patch.object(server, "_refuse_foreign_client_ref", AsyncMock(return_value=None)):
        response = http.post("/sessions/record-turn", json={
            "client_type": "terminal", "client_ref": "terminal-sess-1",
            "role": "user", "content": "hi",
            names["conversation_id"]: "conv-7",
        })

    assert response.status_code == 200, response.text
    assert record.await_args.kwargs["claude_sid"] == "conv-7"


@NAMES
def test_firing_a_rotation_takes_the_conversation_under_either_name(client, names):
    http, server = client
    fire = AsyncMock(return_value={"ok": True})

    with patch.object(server.session_mgr, "fire_rotation", fire), \
            patch.object(server, "_refuse_foreign_client_ref", AsyncMock(return_value=None)):
        response = http.post("/sessions/fire-rotation", json={
            "client_type": "terminal", "client_ref": "terminal-sess-1",
            "prompt": "go on", names["conversation_id"]: "conv-7",
        })

    assert response.status_code == 200, response.text
    assert fire.await_args.kwargs["claude_sid"] == "conv-7"


@NAMES
def test_a_command_takes_the_surface_under_either_name(client, names):
    http, server = client
    listing = AsyncMock(return_value=[])

    with patch.object(server.session_mgr, "list_selectable_sessions", listing):
        response = http.post("/command", json={
            "content": "/sessions", names["surface"]: "telegram:mind-1",
            names["surface_id"]: "chat-9", "client_ref": "chat-9", "mind_id": "mind-1",
        })

    assert response.status_code == 200, response.text
    assert listing.await_args.kwargs["client_type"] == "telegram:mind-1"
    assert listing.await_args.kwargs["owner_ref"] == "chat-9"
