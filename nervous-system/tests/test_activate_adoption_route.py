"""`POST /sessions/{id}/activate` carries a surface, or it is not an adoption.

A surface that reaches a conversation living in a tmux pane must release
that pane before it speaks, or comms spawns a second `--resume` process
beside the one already holding the transcript. That release is driven by
the surface fields, and a request model that quietly drops them turns every
adoption over HTTP into a plain activation that looks identical in the
response and breaks the invariant silently.

Checked against the row the real session manager stored. The mind is the
one thing stubbed: releasing a pane and spawning a process are calls to it.
"""

import importlib
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from comms_pg_fixture import (  # noqa: F401 - fixtures
    add_turn, comms_db, fetchrow, scratch_dsn, seed_session,
)


#: These tests are about their own subject, not about authorization, so they
#: present the admin credential. They used to clear every token instead and
#: rely on the gateway serving open when unconfigured — a bypass that is gone,
#: because a gateway that cannot establish who is calling must refuse rather
#: than answer the LAN.
GRILL_ADMIN = "admin-credential-for-tests"  # secret-guard: allow


@pytest.fixture()
def client(monkeypatch, comms_db):  # noqa: F811 - the imported fixture
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", GRILL_ADMIN)

    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app, headers={"Authorization": f"Bearer {GRILL_ADMIN}"}) as http:
        async def _seed():
            await seed_session("sess-1", surface="web", surface_id="terminal",
                               status="idle", mind_id="ada")
            # A conversation that took a turn, so waking it is a resume.
            await add_turn("sess-1", "user", "hello")

        http.portal.call(_seed)
        mgr = server_module.session_mgr
        with patch.object(mgr, "release_on_mind", AsyncMock()), \
                patch.object(mgr, "_spawn", AsyncMock()):
            yield http


def _stored(http) -> dict:
    return http.portal.call(
        fetchrow, "SELECT surface, surface_id FROM core.sessions WHERE id = 'sess-1'"
    )


def test_the_surface_named_takes_the_conversation(client):
    response = client.post(
        "/sessions/sess-1/activate",
        # Deliberately different from the client pair. Identical values
        # cannot tell a route that forwards the surface from one that
        # forwards the client fields twice.
        json={
            "client_type": "browser",
            "client_ref": "tab-7",
            "surface": "design",
            "surface_id": "design-1",
        },
    )

    assert response.status_code == 200, response.text
    assert _stored(client) == {"surface": "design", "surface_id": "design-1"}


def test_a_request_with_no_surface_is_still_a_plain_activation(client):
    response = client.post(
        "/sessions/sess-1/activate",
        json={"client_type": "telegram", "client_ref": "chat-1"},
    )

    assert response.status_code == 200, response.text
    assert _stored(client) == {"surface": "web", "surface_id": "terminal"}
