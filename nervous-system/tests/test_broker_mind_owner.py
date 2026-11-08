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

import pytest
from fastapi.testclient import TestClient

from comms_broker_pg import broker_db, put_mind, scratch_dsn  # noqa: F401 - fixtures

MIND_A = "11111111-1111-4111-8111-111111111111"
ADMIN = {"Authorization": "Bearer admin-bearer"}


#: These tests are about their own subject, not about authorization, so they
#: present the admin credential. They used to clear every token instead and
#: rely on the gateway serving open when unconfigured — a bypass that is gone,
#: because a gateway that cannot establish who is calling must refuse rather
#: than answer the LAN.
GRILL_ADMIN = "admin-bearer"  # secret-guard: allow


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
def app_client(monkeypatch, broker_db):
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-bearer")

    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app, headers={"Authorization": f"Bearer {GRILL_ADMIN}"}) as client:
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


def _pre_owner_row() -> None:
    """A mind registered before ownership existed: its owner is still unset."""
    asyncio.run(put_mind(
        MIND_A, "alpha", gateway_url="http://alpha:8420", model="sonnet",
        harness="claude_cli", session_token="alpha-token",  # secret-guard: allow
        voice="af_heart", voice_engine="kokoro",
    ))


def test_a_pre_owner_mind_still_registers_unchanged_after_the_upgrade(
    monkeypatch, broker_db
) -> None:
    """Rollout in any order: new comms, old mind, no owner stamped yet."""
    _pre_owner_row()
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-bearer")
    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app, headers={"Authorization": f"Bearer {GRILL_ADMIN}"}) as client:
        response = client.post(
            "/broker/minds", headers=ADMIN, json=_registration(model="opus")
        )
        assert response.status_code == 200
        row = _listed(client)[MIND_A]
        assert row["model"] == "opus"
        assert row["owner"] is None


def test_a_pre_owner_row_registering_with_an_owner_is_listed_under_it(
    monkeypatch, broker_db
) -> None:
    _pre_owner_row()
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-bearer")
    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app, headers={"Authorization": f"Bearer {GRILL_ADMIN}"}) as client:
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
