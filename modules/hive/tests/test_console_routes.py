"""Route tests for the consumer-facing Hive control center."""

import os
import sys
from unittest.mock import AsyncMock, patch

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


def test_home_redirects_authenticated_owner_to_overview(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/overview"


def test_console_pages_require_authentication(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    for path in ("/overview", "/setup", "/minds", "/outposts", "/tools", "/activity"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login?next="), path


def test_console_pages_render_the_new_application_shell(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    for path, marker in (
        ("/overview", "System overview"),
        ("/setup", "Set up your Hive"),
        ("/minds", "Minds"),
        ("/outposts", "Outposts"),
        ("/tools", "Hive Tools"),
        ("/activity", "Activity"),
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert marker in response.text, path
        assert "console-sidebar" in response.text, path


def test_overview_consumers_normalize_live_data_before_rendering(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    for path in ("/overview", "/setup", "/settings/connections"):
        response = client.get(path)
        assert response.status_code == 200
        assert "normalizeOverview(await h.requestJson" in response.text


def test_console_javascript_exports_overview_normalization():
    source = (main_mod.BASE_DIR / "static" / "console.js").read_text()

    assert "function normalizeOverview(data)" in source
    assert "last_activity" in source
    assert "Hive Comms returned live registry and session data." in source
    assert "normalizeOverview: normalizeOverview" in source


def test_mind_detail_renders_for_authenticated_owner(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.get("/minds/b9f2")

    assert response.status_code == 200
    assert "Mind details" in response.text
    assert "b9f2" in response.text


def test_settings_redirects_to_general_and_scoped_pages_render(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    root = client.get("/settings", follow_redirects=False)
    assert root.status_code == 303
    assert root.headers["location"] == "/settings/general"

    for section in ("general", "access", "connections", "voice", "advanced"):
        response = client.get(f"/settings/{section}")
        assert response.status_code == 200
        assert f'data-settings-section="{section}"' in response.text


def test_unknown_settings_section_is_not_found(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    assert client.get("/settings/not-real").status_code == 404


def test_overview_api_combines_minds_sessions_and_service_health(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    minds = [{"name": "ada", "id": "mind-1", "last_seen": 123}]
    sessions = [
        {
            "id": "session-1",
            "mind_id": "mind-1",
            "status": "active",
            "last_active": 456,
        },
        {"id": "session-2", "mind_id": "mind-1", "status": "closed", "last_active": 400},
    ]
    health = AsyncMock(return_value={"reachable": True, "detail": {"status": "ok"}})
    monkeypatch.setenv("COMMS_BEARER_TOKEN", "comms-test-token")
    monkeypatch.setenv("LUCENT_BEARER_TOKEN", "lucent-test-token")

    with (
        patch.object(main_mod, "_broker_minds_safe", new=AsyncMock(return_value=minds)),
        patch.object(main_mod, "_gateway_json", new=AsyncMock(return_value=sessions)),
        patch.object(main_mod, "_service_health", new=health),
        patch.object(main_mod, "hive_tools_status", return_value={"present": True, "path": "/tools"}),
    ):
        response = client.get("/api/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["counts"] == {"minds": 1, "sessions": 1}
    assert body["services"]["comms"]["reachable"] is True
    assert body["services"]["lucent"]["reachable"] is True
    assert body["services"]["tools"]["installed"] is True
    assert body["minds"][0]["last_activity"] == 456
    assert body["minds"][0]["activity_status"] == "active"
    assert health.await_args_list[0].kwargs["headers"] == {
        "Authorization": "Bearer comms-test-token"
    }
    assert health.await_args_list[1].kwargs["headers"] == {
        "Authorization": "Bearer lucent-test-token"
    }


def test_session_activity_does_not_treat_registration_as_a_heartbeat():
    registry = [
        {"name": "nagatha", "id": "mind-1", "last_seen": 100},
        {"name": "bob", "id": "mind-2", "last_seen": 200},
    ]
    sessions = [
        {"mind_id": "mind-1", "status": "closed", "last_active": 300},
        {"mind_id": "mind-1", "status": "running", "last_active": 400},
    ]

    rows = main_mod._add_session_activity(registry, sessions)

    by_name = {row["name"]: row for row in rows}
    assert by_name["nagatha"]["last_activity"] == 400
    assert by_name["nagatha"]["active_session_count"] == 1
    assert by_name["nagatha"]["activity_status"] == "active"
    assert by_name["bob"]["last_activity"] is None
    assert by_name["bob"]["activity_status"] == "registered"


def test_outposts_api_returns_remote_minds_as_managed_installations(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    registry = [
        {
            "name": "arnold",
            "id": "remote-1",
            "gateway_url": "http://deadpool:8421",
            "last_seen": 123,
            "local_config": {"config": {"remote": True, "role": "operator"}},
        },
        {
            "name": "ada",
            "id": "local-1",
            "gateway_url": "http://ada:8420",
            "last_seen": 123,
            "local_config": {"config": {"remote": False}},
        },
    ]
    with (
        patch.object(main_mod, "_broker_minds_safe", new=AsyncMock(return_value=[])),
        patch.object(main_mod, "_gateway_json", new=AsyncMock(return_value=[])),
        patch.object(main_mod, "build_mind_registry", return_value=registry),
    ):
        response = client.get("/api/outposts")

    assert response.status_code == 200
    assert [row["name"] for row in response.json()["outposts"]] == ["arnold"]
    assert response.json()["outposts"][0]["host"] == "deadpool"


def test_outpost_bootstrap_validates_input_and_never_includes_credentials(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/outposts/bootstrap",
        json={
            "name": "atlas",
            "role": "satellite",
            "deployment": "container",
            "harness": "codex_cli_codex",
            "surfaces": ["telegram"],
            "port": 8421,
        },
    )

    assert response.status_code == 200
    command = response.json()["command"]
    assert "hive-outpost" in command
    assert "atlas" in command
    assert "token" not in command.lower()
    assert "password" not in command.lower()

    invalid = client.post(
        "/api/outposts/bootstrap",
        json={
            "name": "../bad",
            "role": "operator",
            "deployment": "systemd",
            "harness": "claude_cli_claude",
            "surfaces": [],
            "port": 8421,
        },
    )
    assert invalid.status_code == 400


def test_tools_api_returns_capability_catalog(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    status = {
        "present": True,
        "path": "/tools",
        "capabilities": [{"id": "gmail", "name": "Gmail", "category": "Communication"}],
    }
    with (
        patch.object(main_mod, "hive_tools_status", return_value=status),
        patch.object(
            main_mod,
            "_service_health",
            new=AsyncMock(return_value={"reachable": True, "detail": {"status": "ok"}}),
        ),
    ):
        response = client.get("/api/tools")

    assert response.status_code == 200
    assert response.json()["capabilities"][0]["name"] == "Gmail"
    assert response.json()["health"]["reachable"] is True
