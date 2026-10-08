"""The HTTP boundary a mind's engine crosses to reach the broker row.

`broker.register_mind` storing it is asserted directly in
`test_broker_mind_voice_engine.py`. These are the routes, which are the only
way a mind or the console ever reaches that function — and with nothing
crossing the boundary, dropping the field from either request model left all
403 tests green while every registration silently discarded it.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

MIND_A = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.setenv("BROKER_DB_PATH", str(tmp_path / "broker.db"))
    monkeypatch.setenv("SESSIONS_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.delenv("COMMS_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-bearer")

    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app) as client:
        yield client, server_module


def _register(client, **extra):
    body = {
        "mind_id": MIND_A,
        "name": "alpha",
        "gateway_url": "http://alpha:8420",
        "model": "sonnet",
        "harness": "claude_cli",
    }
    body.update(extra)
    return client.post(
        "/broker/minds",
        headers={"Authorization": "Bearer admin-bearer"},
        json=body,
    )


def _listed(client):
    rows = client.get("/broker/minds").json()
    rows = rows.get("minds", rows) if isinstance(rows, dict) else rows
    return [row for row in rows if row.get("name") == "alpha"][0]


def test_registration_carries_the_engine_and_the_voice(app_client) -> None:
    client, _ = app_client

    assert _register(client, voice_engine="kokoro", voice="af_heart").status_code == 200

    row = _listed(client)
    assert row["voice_engine"] == "kokoro"
    assert row["voice"] == "af_heart"


def test_the_listing_reports_the_engine_to_the_service_token(app_client) -> None:
    """Every speaking surface reads this listing; it is how they route."""
    client, _ = app_client
    _register(client, voice_engine="chatterbox", voice="voice_ref.wav")

    assert _listed(client)["voice_engine"] == "chatterbox"


def test_a_re_registration_omitting_the_engine_leaves_it_alone(app_client) -> None:
    """A mind running an older build must not blank what the console set."""
    client, _ = app_client
    _register(client, voice_engine="kokoro")
    _register(client)

    assert _listed(client)["voice_engine"] == "kokoro"


def test_a_re_registration_changing_the_engine_moves_the_row(app_client) -> None:
    client, _ = app_client
    _register(client, voice_engine="chatterbox")
    _register(client, voice_engine="kokoro")

    assert _listed(client)["voice_engine"] == "kokoro"


def test_the_console_can_set_the_engine_on_the_row(app_client) -> None:
    """`PUT /broker/minds/{name}` is the write the console makes after the
    mind's own file has taken."""
    client, _ = app_client
    _register(client, voice_engine="chatterbox")

    response = client.put(
        "/broker/minds/alpha",
        headers={"Authorization": "Bearer admin-bearer"},
        json={"voice_engine": "kokoro", "voice": "af_heart"},
    )

    assert response.status_code == 200
    row = _listed(client)
    assert row["voice_engine"] == "kokoro"
    assert row["voice"] == "af_heart"


def test_a_partial_console_write_does_not_blank_the_engine(app_client) -> None:
    client, _ = app_client
    _register(client, voice_engine="kokoro")

    client.put(
        "/broker/minds/alpha",
        headers={"Authorization": "Bearer admin-bearer"},
        json={"model": "opus"},
    )

    row = _listed(client)
    assert row["voice_engine"] == "kokoro"
    assert row["model"] == "opus"
