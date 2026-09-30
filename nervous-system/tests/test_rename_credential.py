"""Renaming a conversation is its own small capability, guarded on its own.

Naming used to live behind a token that renamed a conversation and did nothing
else — chosen deliberately, because guarding the smallest capability in the
system with the largest key in it is backwards. Moving names onto the session
row must not quietly promote that capability to `COMMS_BEARER_TOKEN`, which
every surface bot and every mind container on this hive holds, including the
ones on the kids' machines.
"""
from __future__ import annotations

import importlib
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

SERVICE = {"Authorization": "Bearer service-bearer"}


def _seed_live_session(db_path: str, session_id: str = "sess-1") -> str:
    """One running session to rename, written straight into the store.

    The manager has already created the schema by the time this runs; a second
    connection to the same file is the cheapest way to put a row there without
    a broker entry and a mind container behind it.
    """
    now = time.time()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO sessions (id, owner_type, owner_ref, model, claude_sid,
                                     created_at, last_active, status, mind_id)
               VALUES (?, 'telegram', '123', 'opus', 'conv-1', ?, ?, 'running', 'ada')""",
            (session_id, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return session_id


@pytest.fixture
def client(monkeypatch, tmp_path):
    db = str(tmp_path / "sessions.db")
    monkeypatch.setenv("BROKER_DB_PATH", str(tmp_path / "broker.db"))
    monkeypatch.setenv("SESSIONS_DB_PATH", db)
    monkeypatch.setenv("COMMS_BEARER_TOKEN", "service-bearer")
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-bearer")
    monkeypatch.setenv("COMMS_RENAME_TOKEN", "rename-only")

    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app) as test_client:
        _seed_live_session(db)
        yield test_client


def test_the_service_token_alone_cannot_rename_a_conversation(client) -> None:
    """R8: the key every bot and every kid's box holds gains nothing here.

    Breaks the moment the route is guarded only by the global bearer gate,
    which is what moving the write onto comms does by default.
    """
    response = client.put(
        "/sessions/sess-1/name", headers=SERVICE, json={"name": "Health"}
    )
    assert response.status_code == 401


def test_the_rename_token_renames_the_conversation(client) -> None:
    """R8: and the small credential does open the one thing it is for."""
    response = client.put(
        "/sessions/sess-1/name",
        headers={**SERVICE, "X-Rename-Token": "rename-only"},
        json={"name": "Health"},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Health"


def test_the_rename_token_opens_nothing_else(client) -> None:
    """R8: rename-only means it is not a way past the service gate.

    Breaks if the rename credential is ever added to `require_bearer`.
    """
    response = client.get("/sessions", headers={"Authorization": "Bearer rename-only"})
    assert response.status_code == 401


def test_renaming_refuses_when_no_rename_credential_is_configured(
    monkeypatch, tmp_path
) -> None:
    """R8: unset refuses, where the service gate deliberately bypasses.

    The service token bypasses when unset so a fresh container can boot before
    its secrets are wired. The same shape here would mean one missing variable
    lets anything on the LAN rename any mind's conversations. Breaks if this
    guard is written like that one.
    """
    db = str(tmp_path / "sessions.db")
    monkeypatch.setenv("BROKER_DB_PATH", str(tmp_path / "broker.db"))
    monkeypatch.setenv("SESSIONS_DB_PATH", db)
    monkeypatch.setenv("COMMS_BEARER_TOKEN", "service-bearer")
    monkeypatch.delenv("COMMS_ADMIN_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("COMMS_RENAME_TOKEN", raising=False)

    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app) as test_client:
        _seed_live_session(db)
        response = test_client.put(
            "/sessions/sess-1/name", headers=SERVICE, json={"name": "Health"}
        )
    assert response.status_code == 503


def test_a_rename_addressed_to_a_retired_conversation_is_refused_by_the_route(
    client,
) -> None:
    """The refusal reaches a caller as a refusal, not as success.

    409 rather than 404: the name is fine and the session exists — the
    conversation it was addressed to has moved on. Breaks if the route folds
    this into the shape that means "no such session", which sends the caller
    looking for the wrong fault.
    """
    client.put(
        "/sessions/sess-1/name",
        headers={**SERVICE, "X-Rename-Token": "rename-only"},
        json={"name": "Health"},
    )
    client.delete("/sessions/sess-1", headers=SERVICE)

    response = client.put(
        "/sessions/sess-1/name",
        headers={**SERVICE, "X-Rename-Token": "rename-only"},
        json={"name": "Later"},
    )
    assert response.status_code == 409


def test_a_hive_with_an_admin_token_but_no_rename_token_says_renaming_is_off(
    monkeypatch, tmp_path
) -> None:
    """R8: the sentence naming the misconfiguration has to be reachable.

    Every hive here has an admin token, so gating the 503 on both tokens being
    absent made it unreachable: a deployment that simply never set the rename
    token answered 401 to every surface, and all three of them reported a
    network fault. Breaks if the guard goes back to requiring both to be unset.
    """
    db = str(tmp_path / "sessions.db")
    monkeypatch.setenv("BROKER_DB_PATH", str(tmp_path / "broker.db"))
    monkeypatch.setenv("SESSIONS_DB_PATH", db)
    monkeypatch.setenv("COMMS_BEARER_TOKEN", "service-bearer")
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-bearer")
    monkeypatch.delenv("COMMS_RENAME_TOKEN", raising=False)

    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app) as test_client:
        _seed_live_session(db)
        refused = test_client.put(
            "/sessions/sess-1/name",
            headers={**SERVICE, "X-Rename-Token": "some-other-secret"},
            json={"name": "Health"},
        )
        # The operator still gets in with the admin token, which is why the
        # admin check runs before this one.
        allowed = test_client.put(
            "/sessions/sess-1/name",
            headers={**SERVICE, "X-Rename-Token": "admin-bearer"},
            json={"name": "Health"},
        )

    assert refused.status_code == 503
    assert allowed.status_code == 200


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
        headers={**SERVICE, "X-Rename-Token": "rename-only"},
        json={"name": "Fittimus Maximus", "color": "#3481cc"},
    )

    response = client.get("/sessions/names", headers=SERVICE)

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
    assert client.get("/sessions/names", headers=SERVICE).status_code == 200
    assert client.get("/sessions/names").status_code == 401
