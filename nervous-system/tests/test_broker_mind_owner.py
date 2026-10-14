"""Every mind belongs to exactly one user, and comms is where that is held.

Ownership is keyed by `mind_id`, not by the display name. A registration may
omit the owner — a known mind keeps the one it has, the way it keeps its
session token — but a mind comms has never seen cannot register without one,
and a registration naming a different owner than the stored one keeps the
stored owner: only the admin route moves a mind between users.
"""
from __future__ import annotations

import asyncio
import importlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

from comms import broker

MIND_A = "11111111-1111-4111-8111-111111111111"
ADMIN = {"Authorization": "Bearer admin-bearer"}


def _registration(**overrides) -> dict:
    body = {
        "mind_id": MIND_A, "name": "alpha",
        "gateway_url": "http://alpha:8420", "model": "sonnet",
        "harness": "claude_cli",
    }
    body.update(overrides)
    return body


def _listed(client) -> dict:
    return {row["id"]: row for row in client.get("/broker/minds").json()}


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.setenv("BROKER_DB_PATH", str(tmp_path / "broker.db"))
    monkeypatch.setenv("SESSIONS_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.delenv("COMMS_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-bearer")

    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app) as client:
        yield client


def test_an_unknown_mind_registering_without_an_owner_is_refused(app_client) -> None:
    response = app_client.post("/broker/minds", headers=ADMIN, json=_registration())
    assert response.status_code == 422
    assert MIND_A not in _listed(app_client)


def test_a_mind_registering_with_an_owner_is_listed_under_it_lowercased(app_client) -> None:
    response = app_client.post(
        "/broker/minds", headers=ADMIN, json=_registration(owner=" Sloan ")
    )
    assert response.status_code == 200
    assert response.json()["owner"] == "sloan"
    assert _listed(app_client)[MIND_A]["owner"] == "sloan"


def test_a_known_mind_re_registering_without_an_owner_keeps_its_owner(app_client) -> None:
    app_client.post("/broker/minds", headers=ADMIN, json=_registration(owner="sloan"))
    response = app_client.post(
        "/broker/minds", headers=ADMIN, json=_registration(model="opus")
    )
    assert response.status_code == 200
    assert _listed(app_client)[MIND_A]["owner"] == "sloan"
    assert _listed(app_client)[MIND_A]["model"] == "opus"


def test_a_registration_naming_a_different_owner_lands_with_the_stored_owner_kept(
    app_client, caplog
) -> None:
    app_client.post("/broker/minds", headers=ADMIN, json=_registration(owner="sloan"))
    with caplog.at_level("WARNING", logger="hive-mind.broker"):
        response = app_client.post(
            "/broker/minds", headers=ADMIN,
            json=_registration(owner="aiden", model="opus"),
        )
    assert response.status_code == 200
    row = _listed(app_client)[MIND_A]
    assert row["owner"] == "sloan"
    assert row["model"] == "opus"
    assert any(r.getMessage() == "mind.owner_ignored" and r.levelname == "WARNING"
               for r in caplog.records)


def test_a_mind_moved_by_the_admin_keeps_registering_under_its_new_owner(
    app_client,
) -> None:
    app_client.post("/broker/minds", headers=ADMIN, json=_registration(owner="sloan"))
    app_client.put(f"/broker/minds/{MIND_A}/owner", headers=ADMIN, json={"owner": "aiden"})
    response = app_client.post(
        "/broker/minds", headers=ADMIN,
        json=_registration(owner="sloan", model="opus"),
    )
    assert response.status_code == 200
    row = _listed(app_client)[MIND_A]
    assert row["owner"] == "aiden"
    assert row["model"] == "opus"


def test_the_same_owner_in_another_case_is_not_a_conflict(app_client) -> None:
    app_client.post("/broker/minds", headers=ADMIN, json=_registration(owner="sloan"))
    response = app_client.post(
        "/broker/minds", headers=ADMIN, json=_registration(owner="SLOAN")
    )
    assert response.status_code == 200
    assert response.json()["owner"] == "sloan"


def test_the_admin_route_moves_a_mind_to_another_owner(app_client) -> None:
    app_client.post("/broker/minds", headers=ADMIN, json=_registration(owner="sloan"))
    response = app_client.put(
        f"/broker/minds/{MIND_A}/owner", headers=ADMIN, json={"owner": "Aiden"}
    )
    assert response.status_code == 200
    assert response.json()["owner"] == "aiden"
    assert _listed(app_client)[MIND_A]["owner"] == "aiden"


def test_the_owner_route_refuses_a_caller_without_the_admin_token(app_client) -> None:
    app_client.post("/broker/minds", headers=ADMIN, json=_registration(owner="sloan"))
    response = app_client.put(
        f"/broker/minds/{MIND_A}/owner",
        headers={"Authorization": "Bearer wrong"}, json={"owner": "aiden"},
    )
    assert response.status_code == 401
    assert _listed(app_client)[MIND_A]["owner"] == "sloan"


def test_the_owner_route_answers_404_for_an_unknown_mind(app_client) -> None:
    response = app_client.put(
        f"/broker/minds/{MIND_A}/owner", headers=ADMIN, json={"owner": "aiden"}
    )
    assert response.status_code == 404
    assert MIND_A in response.text


def test_the_owner_route_refuses_a_blank_owner(app_client) -> None:
    app_client.post("/broker/minds", headers=ADMIN, json=_registration(owner="sloan"))
    response = app_client.put(
        f"/broker/minds/{MIND_A}/owner", headers=ADMIN, json={"owner": "  "}
    )
    assert response.status_code == 422
    assert _listed(app_client)[MIND_A]["owner"] == "sloan"


_PRE_OWNER_SCHEMA = """
CREATE TABLE minds (
    mind_id        TEXT NOT NULL UNIQUE,
    name           TEXT PRIMARY KEY,
    gateway_url    TEXT NOT NULL,
    model          TEXT NOT NULL,
    harness        TEXT NOT NULL,
    registered_at  REAL NOT NULL,
    last_seen      REAL NOT NULL,
    session_token  TEXT,
    voice          TEXT,
    voice_engine   TEXT
);
"""


def _legacy_db(path) -> None:
    con = sqlite3.connect(path)
    con.executescript(_PRE_OWNER_SCHEMA)
    con.execute(
        "INSERT INTO minds VALUES (?, 'alpha', 'http://alpha:8420', 'sonnet', "
        "'claude_cli', 1.0, 1.0, 'alpha-token', 'af_heart', 'kokoro')",
        (MIND_A,),
    )
    con.commit()
    con.close()


def test_startup_adds_the_owner_column_without_losing_rows(tmp_path) -> None:
    path = tmp_path / "broker.db"
    _legacy_db(path)

    async def scenario():
        db = await broker.init_db(str(path))
        try:
            return await broker.get_registered_minds(db)
        finally:
            await db.close()

    rows = asyncio.run(scenario())
    assert len(rows) == 1
    assert rows[0]["id"] == MIND_A
    assert rows[0]["voice_engine"] == "kokoro"
    assert rows[0]["owner"] is None
    assert asyncio.run(_session_token(path)) == "alpha-token"  # secret-guard: allow


async def _session_token(path):
    db = await broker.init_db(str(path))
    try:
        return await broker.get_mind_session_token(db, MIND_A)
    finally:
        await db.close()


def test_a_pre_owner_mind_still_registers_unchanged_after_the_upgrade(
    monkeypatch, tmp_path
) -> None:
    """Rollout in any order: new comms, old mind, no owner stamped yet."""
    _legacy_db(tmp_path / "broker.db")
    monkeypatch.setenv("BROKER_DB_PATH", str(tmp_path / "broker.db"))
    monkeypatch.setenv("SESSIONS_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.delenv("COMMS_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-bearer")
    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app) as client:
        response = client.post(
            "/broker/minds", headers=ADMIN, json=_registration(model="opus")
        )
        assert response.status_code == 200
        row = _listed(client)[MIND_A]
        assert row["model"] == "opus"
        assert row["owner"] is None


def test_a_pre_owner_row_registering_with_an_owner_is_listed_under_it(
    monkeypatch, tmp_path
) -> None:
    _legacy_db(tmp_path / "broker.db")
    monkeypatch.setenv("BROKER_DB_PATH", str(tmp_path / "broker.db"))
    monkeypatch.setenv("SESSIONS_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.delenv("COMMS_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-bearer")
    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app) as client:
        response = client.post(
            "/broker/minds", headers=ADMIN, json=_registration(owner="Sloan")
        )
        assert response.status_code == 200
        assert _listed(client)[MIND_A]["owner"] == "sloan"
        again = client.post(
            "/broker/minds", headers=ADMIN, json=_registration(owner="aiden")
        )
        assert again.status_code == 200
        assert _listed(client)[MIND_A]["owner"] == "sloan"
