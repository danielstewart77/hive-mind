"""Renaming a conversation is its own small capability, guarded on its own.

Naming used to live behind a token that renamed a conversation and did nothing
else — chosen deliberately, because guarding the smallest capability in the
system with the largest key in it is backwards. Moving names onto the session
row must not quietly promote that capability to a credential that reaches
everything, so the rename token stays its own and is not accepted anywhere
else, and nothing else is accepted for a rename.

The caller that stands in for "a credential that reaches everything" is now
the admin one: the shared service token every surface bot and every mind
container used to hold is no longer a credential at all.
"""
from __future__ import annotations

import asyncio
import importlib
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from comms_broker_pg import admin_person_key  # noqa: E402
from comms_broker_pg import (  # noqa: F401 - fixtures
    broker_db,
    execute,
    fetchval,
    put_credential_row,
    put_mind,
    scratch_dsn,
)

ADMIN = "admin-bearer"  # secret-guard: allow
#: Whose minds these are. `owns_mind` resolves a conversation to a person,
#: and a mind credential answers for its owner.
OPERATOR = "daniel"
REACHES_EVERYTHING = {"Authorization": f"Bearer {ADMIN}"}


def _seed_live_session(session_id: str = "sess-1") -> str:
    """One running session to rename, written straight into the store on a
    connection of its own — the app's pool lives on the app's loop."""
    async def seed():
        await put_mind("ada")
        await execute(
            "INSERT INTO core.sessions (id, surface, surface_id, model, "
            "conversation_id, created_at, last_active, status, mind_id) "
            "VALUES ($1, 'telegram', '123', 'opus', 'conv-1', "
            "extract(epoch from now()), extract(epoch from now()), "
            "'running', 'ada')",
            session_id,
        )

    asyncio.run(seed())
    return session_id


@pytest.fixture
def client(monkeypatch, broker_db):
    admin_person_key(ADMIN)

    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app) as test_client:
        _seed_live_session()
        yield test_client


def test_an_unknown_key_cannot_rename_a_conversation(client) -> None:
    """A rename is asked with the key of whoever asks; a key the one record
    does not hold renames nothing."""
    response = client.put(
        "/sessions/sess-1/name",
        headers={"Authorization": "Bearer a-key-nobody-holds"},  # secret-guard: allow
        json={"name": "Health"},
    )
    assert response.status_code == 401


def test_an_administrators_own_key_renames_the_conversation(client) -> None:
    response = client.put(
        "/sessions/sess-1/name", headers=REACHES_EVERYTHING, json={"name": "Health"},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Health"


def test_a_rename_addressed_to_a_retired_conversation_is_refused_by_the_route(
    client,
) -> None:
    """The refusal reaches a caller as a refusal, not as success.

    409 rather than 404: the name is fine and the session exists — the
    conversation it was addressed to has moved on. Breaks if the route folds
    this into the shape that means "no such session" — which, since ending a
    conversation removes its row, is now exactly what it is. A row still
    marked closed in the store is the one that answers 409.
    """
    client.put(
        "/sessions/sess-1/name",
        headers={**REACHES_EVERYTHING},
        json={"name": "Health"},
    )
    client.delete("/sessions/sess-1", headers=REACHES_EVERYTHING)

    response = client.put(
        "/sessions/sess-1/name",
        headers={**REACHES_EVERYTHING},
        json={"name": "Later"},
    )
    assert response.status_code == 404


def test_every_name_the_hive_holds_is_readable_in_one_call(client) -> None:
    """The one read path the terminal, the picker and the coach tab all consume.

    An empty answer draws every conversation on the hive unnamed, which is the
    exact symptom this change exists to fix — and every consumer stubs the
    gateway, so nothing else in any repo would notice. Breaks if the method
    stops reading the columns, and breaks if the route is declared after
    `/sessions/{session_id}`, where the wildcard swallows the literal and
    `names` is read as a session id.
    """
    client.put(
        "/sessions/sess-1/name",
        headers={**REACHES_EVERYTHING},
        json={"name": "Fittimus Maximus", "color": "#3481cc"},
    )

    response = client.get("/sessions/names", headers=REACHES_EVERYTHING)

    assert response.status_code == 200
    assert response.json()["sess-1"] == {
        "name": "Fittimus Maximus", "color": "#3481cc"
    }


def test_the_names_listing_answers_the_service_token(client) -> None:
    """Reading a name is not the capability worth guarding; changing one is.

    Every surface bot holds the service token and every one of them draws a
    picker. Breaks if this route is put behind the rename or admin credential,
    which would leave the picker with nothing to show.
    """
    assert client.get("/sessions/names", headers=REACHES_EVERYTHING).status_code == 200
    assert client.get("/sessions/names").status_code == 401


# ---------------------------------------------------------------------------
# Naming a conversation as it is created is the same capability
# ---------------------------------------------------------------------------
def _a_mind_with_its_own_credential(mind_id: str = "bob") -> str:
    """A second mind, owned by the operator, holding a credential of its own.

    The digest is computed by `user_access.digest` rather than by this file,
    so a change to how a credential is stored fails the test instead of
    passing against a hash written here. The mind declares no harness, so no
    harness listing is asked of it on the way through the create.
    """
    from comms import user_access

    async def seed():
        await put_mind(mind_id, harness="", owner=OPERATOR)
        await put_credential_row(
            holder=mind_id, username=OPERATOR, role=user_access.MIND,
            token_sha=user_access.digest(_own_credential(mind_id)),
        )

    asyncio.run(seed())
    return mind_id


def _own_credential(mind_id: str) -> str:
    return f"{mind_id}-own-credential"  # secret-guard: allow


@pytest.fixture
def creating(client):
    """The create route with the spawn and the prompt composition stubbed —
    the transport to the mind and to lucent, and nothing nearer."""
    async def blocks(**_kw):
        return "<soul>seed</soul>"

    async def spawn(_self, _session_id, _model, **_kw):
        return None

    from comms.sessions import SessionManager

    with patch("comms.bootstrap_loader.compose_prompt_blocks", blocks), \
            patch.object(SessionManager, "_spawn", spawn):
        yield client


def _create(http, mind_id, token, **body):
    return http.post(
        "/sessions",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "surface": "web", "surface_id": "terminal",
            "client_ref": f"staff-{body.get('name', 'bare')}",
            "mind_id": mind_id, **body,
        },
    )


def test_a_mind_names_the_conversation_it_creates_with_its_own_key(creating) -> None:
    """No second credential guards a label: the mind's own key, held to a mind
    its owner owns, is enough."""
    mind = _a_mind_with_its_own_credential()
    token = _own_credential(mind)

    labelled = _create(creating, mind, token, name="deploy-prod")

    assert labelled.status_code == 200, labelled.text
    assert labelled.json()["name"] == "deploy-prod"


def test_a_create_whose_colour_is_not_a_hex_value_is_refused_as_a_bad_value(
    creating,
) -> None:
    """R3: 400 for the value the caller got wrong, and nothing written.

    The route answers this one itself rather than mapping every `ValueError`
    out of `create_session`: no model, an unavailable harness and a mind
    deregistered mid-create all raise that too, and they are not the caller
    sending a bad colour. Breaks if the check leaves the route, which turns
    this into the 500 the manager's refusal becomes on its own.
    """
    mind = _a_mind_with_its_own_credential("dave")

    refused = _create(
        creating, mind, ADMIN, color="red; content:url(x)",
    )

    assert refused.status_code == 400, refused.text
    # Scoped to the create that was refused: this fixture seeds a session of
    # its own, so an empty table would be the wrong thing to ask for.
    assert asyncio.run(
        fetchval("SELECT COUNT(*) FROM core.sessions WHERE mind_id = $1", mind)
    ) == 0


def test_the_credential_route_removes_a_mind_row_only_when_asked_to(client):
    """`remove=true` is what a failed creation's undo sends; without it the
    route keeps the row revoked, which is what a cut-off needs."""
    key = "pasted-route-key-0123456789"  # secret-guard: allow
    put = client.put("/credentials/route-mind", headers=REACHES_EVERYTHING,
                     json={"username": OPERATOR, "token": key, "role": "mind"})
    assert put.status_code == 200, put.text

    revoked = client.delete("/credentials/route-mind", headers=REACHES_EVERYTHING)
    assert revoked.json()["revoked"] is True
    clash = client.put("/credentials/other-mind", headers=REACHES_EVERYTHING,
                       json={"username": OPERATOR, "token": key, "role": "mind"})
    assert clash.status_code == 400

    removed = client.delete("/credentials/route-mind?remove=true", headers=REACHES_EVERYTHING)
    assert removed.json()["removed"] is True
    freed = client.put("/credentials/other-mind", headers=REACHES_EVERYTHING,
                       json={"username": OPERATOR, "token": key, "role": "mind"})
    assert freed.status_code == 200, freed.text
