"""Route tests for the training panel and its Hive Tools proxy."""

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth
import main as main_mod


def _authed_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = auth.create_user("daniel", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
    return client


@pytest.fixture()
def tools_call():
    """Patch the Hive Tools client and expose the recorded call."""
    with patch.object(main_mod, "_hive_tools_request", new_callable=AsyncMock) as mock:
        mock.return_value = {}
        yield mock


def test_training_page_requires_authentication(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    response = TestClient(main_mod.app).get("/training", follow_redirects=False)
    assert response.status_code in (302, 303, 307)


def test_training_page_renders_for_an_authenticated_owner(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    response = client.get("/training")
    assert response.status_code == 200
    assert "Keep per duplicate cluster" in response.text


def test_training_appears_in_the_navigation(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    assert 'href="/training"' in client.get("/overview").text


def test_status_proxies_to_hive_tools(tmp_path, monkeypatch, tools_call):
    tools_call.return_value = {"total": 8809, "by_flag": {"keep": 3024}}
    client = _authed_client(tmp_path, monkeypatch)

    response = client.get("/api/training/status")

    assert response.status_code == 200
    assert response.json()["total"] == 8809
    tools_call.assert_awaited_once_with("GET", "/training/status")


def test_curate_forwards_the_policy_body_with_a_long_timeout(
    tmp_path, monkeypatch, tools_call
):
    """Curation walks the whole corpus; the default 30s client would abort it."""
    tools_call.return_value = {"kept": 3024, "total": 8809}
    client = _authed_client(tmp_path, monkeypatch)

    response = client.post("/api/training/curate", json={"keep_per_cluster": 5})

    assert response.status_code == 200
    kwargs = tools_call.await_args.kwargs
    assert kwargs["json_body"] == {"keep_per_cluster": 5}
    assert kwargs["timeout"] > 30.0


def test_export_forwards_its_body(tmp_path, monkeypatch, tools_call):
    tools_call.return_value = {"train_examples": 2840}
    client = _authed_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/training/export", json={"mode": "reasoning", "name": "v1"}
    )

    assert response.status_code == 200
    assert tools_call.await_args.kwargs["json_body"]["mode"] == "reasoning"


def test_train_requires_a_training_file(tmp_path, monkeypatch, tools_call):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.post("/api/training/train", json={"name": "x"})

    assert response.status_code == 400
    tools_call.assert_not_awaited()


def test_train_forwards_a_valid_request(tmp_path, monkeypatch, tools_call):
    tools_call.return_value = {"can_run": False, "blockers": ["gpu busy"]}
    client = _authed_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/training/train", json={"train_file": "/data/train.jsonl"}
    )

    assert response.status_code == 200
    assert response.json()["blockers"] == ["gpu busy"]


def test_runs_rejects_an_unknown_kind(tmp_path, monkeypatch, tools_call):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.get("/api/training/runs?kind=nonsense")

    assert response.status_code == 400
    tools_call.assert_not_awaited()


def test_runs_clamps_the_limit(tmp_path, monkeypatch, tools_call):
    tools_call.return_value = {"runs": []}
    client = _authed_client(tmp_path, monkeypatch)

    client.get("/api/training/runs?limit=9999")

    assert tools_call.await_args.kwargs["params"]["limit"] == 200


def test_training_endpoints_require_authentication(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    for method, path in (
        ("get", "/api/training/status"),
        ("get", "/api/training/runs"),
        ("post", "/api/training/curate"),
        ("post", "/api/training/export"),
        ("post", "/api/training/train"),
    ):
        kwargs = {"follow_redirects": False}
        if method == "post":
            kwargs["json"] = {}
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code in (302, 303, 307, 401, 403), path
