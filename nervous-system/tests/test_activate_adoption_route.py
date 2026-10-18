"""`POST /sessions/{id}/activate` carries an owner, or it is not an adoption.

A surface that reaches a conversation living in a tmux pane must release
that pane before it speaks, or comms spawns a second `--resume` process
beside the one already holding the transcript. That release is driven by
the owner fields, and a request model that quietly drops them turns every
adoption over HTTP into a plain activation that looks identical in the
response and breaks the invariant silently.
"""

import importlib
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


#: These tests are about their own subject, not about authorization, so they
#: present the admin credential. They used to clear every token instead and
#: rely on the gateway serving open when unconfigured — a bypass that is gone,
#: because a gateway that cannot establish who is calling must refuse rather
#: than answer the LAN.
GRILL_ADMIN = "admin-credential-for-tests"  # secret-guard: allow


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("BROKER_DB_PATH", str(tmp_path / "broker.db"))
    monkeypatch.setenv("SESSIONS_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", GRILL_ADMIN)

    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app, headers={"Authorization": f"Bearer {GRILL_ADMIN}"}) as http:
        yield http, server_module


def test_the_owner_reaches_the_session_manager(client):
    http, server = client
    activate = AsyncMock(return_value={"ok": True})

    with patch.object(server.session_mgr, "activate_session", activate):
        response = http.post(
            "/sessions/sess-1/activate",
            # Deliberately different from the client pair. Identical values
            # cannot tell a route that forwards the owner from one that
            # forwards the client fields twice.
            json={
                "client_type": "browser",
                "client_ref": "tab-7",
                "owner_type": "design",
                "owner_ref": "design-1",
            },
        )

    assert response.status_code == 200
    assert activate.await_args.args[1:] == ("browser", "tab-7")
    assert activate.await_args.kwargs["owner_type"] == "design"
    assert activate.await_args.kwargs["owner_ref"] == "design-1"


def test_a_request_with_no_owner_is_still_a_plain_activation(client):
    http, server = client
    activate = AsyncMock(return_value={"ok": True})

    with patch.object(server.session_mgr, "activate_session", activate):
        response = http.post(
            "/sessions/sess-1/activate",
            json={"client_type": "telegram", "client_ref": "chat-1"},
        )

    assert response.status_code == 200
    assert activate.await_args.kwargs["owner_type"] is None
    assert activate.await_args.kwargs["owner_ref"] is None
