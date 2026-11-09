"""Route tests for the standing-rules dashboard."""

import os
import sys
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

import auth
import main as main_mod
import accounts
from hive_site.testing import run


def _authed_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = run(auth.create_user("daniel", "secret-pass", is_admin=True, replace=True))
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, run(auth.create_session_token(user)))
    return client


def test_rules_page_redirects_when_unauthenticated(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    response = client.get("/rules", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=/rules")


def test_rules_page_renders_for_authed_user(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    with patch.object(main_mod, "_broker_minds_safe", new=AsyncMock(return_value=[])):
        response = client.get("/rules")
    assert response.status_code == 200


def test_api_rules_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    assert client.get("/api/rules").status_code == 401


def test_api_rules_list_merges_tiers(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    standing = [
        {"id": 1, "content": "rule one", "mind_id": "shared", "tier": "standing",
         "source": "always-remember", "data_class": "feedback", "tags": "", "created_at": 100}
    ]
    contextual = [
        {"id": 2, "content": "rule two", "mind_id": "shared", "tier": "contextual",
         "source": "user", "data_class": "feedback", "tags": "", "created_at": 200}
    ]

    async def fake_lucent(method, path, *, params=None, json_body=None):
        del method, json_body
        entries = standing if params.get("tier") == "standing" else contextual
        return {"entries": entries, "total": len(entries)}

    with (
        patch.object(main_mod, "_broker_minds_safe", new=AsyncMock(return_value=[])),
        patch.object(main_mod, "_lucent_request", new=fake_lucent),
    ):
        response = client.get("/api/rules")

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2
    assert data["rules"][0]["tier"] == "standing"


def test_api_rules_create_validates(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    response = client.post("/api/rules", json={"content": "", "mind_id": "shared", "tier": "standing"})
    assert response.status_code == 400

    response = client.post("/api/rules", json={"content": "x", "mind_id": "shared", "tier": "bogus"})
    assert response.status_code == 400

    with patch.object(main_mod, "_lucent_request", new=AsyncMock(return_value={"stored": True})):
        response = client.post(
            "/api/rules",
            json={"content": "x", "mind_id": "shared", "tier": "standing"},
        )
    assert response.status_code == 200


async def test_seed_first_user_is_idempotent(monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    monkeypatch.setenv("HIVE_SEED_USERNAME", "daniel")
    monkeypatch.setenv("HIVE_SEED_PASSWORD", "seed-pass")

    await auth.seed_first_user()
    store = auth.BuiltinStore()
    seeded = await store.get_user("daniel")
    assert seeded["is_admin"] is True

    monkeypatch.setenv("HIVE_SEED_PASSWORD", "different-pass")
    await auth.seed_first_user()
    assert (await store.sign_in("daniel", "seed-pass"))[0][
        "username"] == "daniel"
    with pytest.raises(auth.SignInRefused):
        await store.sign_in("daniel", "different-pass")
