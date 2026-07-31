"""Route tests for the /settings page and its API endpoints."""

import os
import sys
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi import HTTPException

import auth
import main as main_mod


def _authed_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = auth.create_user("daniel", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
    return client


def test_settings_page_redirects_when_unauthenticated(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    response = client.get("/settings", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=/settings")


def test_settings_page_renders_for_authed_user(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    response = client.get("/settings")
    assert response.status_code == 200
    assert "System configuration" in response.text
    assert 'data-settings-section="general"' in response.text


def test_api_settings_minds_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    assert client.get("/api/settings/minds").status_code == 401


def test_api_settings_minds_merges_broker_and_local_config(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    broker_minds = [{"name": "bob", "id": "b9f2", "gateway_url": "http://bob:8420", "model": "qwen3"}]
    sessions = [{"mind_id": "b9f2", "status": "running", "last_active": 456}]
    with (
        patch.object(main_mod, "_broker_minds_safe", new=AsyncMock(return_value=broker_minds)),
        patch.object(main_mod, "_gateway_json", new=AsyncMock(return_value=sessions)),
    ):
        response = client.get("/api/settings/minds")

    assert response.status_code == 200
    minds = response.json()["minds"]
    assert minds[0]["name"] == "bob"
    assert minds[0]["last_activity"] == 456
    assert minds[0]["activity_status"] == "active"


def test_mind_configuration_route_updates_model_and_broker(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    updated = {
        "name": "bob",
        "remote": False,
        "provider": "ollama",
        "default_model": "qwen3:8b",
    }
    gateway = AsyncMock(return_value={"status": "updated"})
    with (
        patch.object(
            main_mod,
            "local_runtime_configuration",
            return_value={"name": "bob", "default_model": "old-model"},
        ),
        patch.object(main_mod, "update_local_runtime_model", return_value=updated),
        patch.object(main_mod, "_gateway_request", new=gateway),
    ):
        response = client.patch(
            "/api/minds/bob/configuration",
            json={"default_model": "qwen3:8b"},
        )

    assert response.status_code == 200
    assert response.json()["configuration"]["default_model"] == "qwen3:8b"
    assert gateway.await_args.args[:2] == ("PUT", "/broker/minds/bob")
    assert gateway.await_args.kwargs["json_body"] == {"model": "qwen3:8b"}


def test_mind_configuration_lists_models_for_current_provider(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    models = [
        {"name": "sonnet", "provider": "anthropic"},
        {"name": "qwen3:8b", "provider": "ollama"},
    ]
    with (
        patch.object(
            main_mod,
            "local_runtime_configuration",
            return_value={
                "name": "bob",
                "harness": "claude_cli",
                "provider": "ollama",
                "default_model": "qwen3:8b",
            },
        ),
        patch.object(main_mod, "_gateway_json", new=AsyncMock(return_value=models)),
    ):
        response = client.get("/api/minds/bob/configuration")

    assert response.status_code == 200
    assert response.json()["models"] == ["qwen3:8b"]


def test_mind_configuration_rolls_back_file_when_broker_update_fails(
    tmp_path, monkeypatch
):
    client = _authed_client(tmp_path, monkeypatch)
    update = patch.object(
        main_mod,
        "update_local_runtime_model",
        side_effect=[
            {"name": "bob", "default_model": "qwen3:8b"},
            {"name": "bob", "default_model": "old-model"},
        ],
    )
    with (
        patch.object(
            main_mod,
            "local_runtime_configuration",
            return_value={"name": "bob", "default_model": "old-model"},
        ),
        patch.object(
            main_mod,
            "_gateway_request",
            new=AsyncMock(side_effect=HTTPException(status_code=502, detail="down")),
        ),
        update as update_mock,
    ):
        response = client.patch(
            "/api/minds/bob/configuration",
            json={"default_model": "qwen3:8b"},
        )

    assert response.status_code == 502
    assert update_mock.call_args_list[1].args == ("bob", "old-model")


def test_mind_restart_submits_hive_tools_approval(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    monkeypatch.setenv("HIVE_TOOLS_TOKEN", "tools-token")
    with (
        patch.object(main_mod, "local_runtime_configuration", return_value={"name": "bob"}),
        patch.object(
            main_mod,
            "_hive_tools_request",
            new=AsyncMock(return_value={"hitl_request_id": "request-1", "status": "pending"}),
        ) as request,
    ):
        response = client.post("/api/minds/bob/restart")

    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert request.await_args.args == ("POST", "/docker/restart")
    assert request.await_args.kwargs["params"]["service"] == "bob"


def test_operation_status_proxies_hive_tools(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    with patch.object(
        main_mod,
        "_hive_tools_request",
        new=AsyncMock(return_value={"status": "approved"}),
    ):
        response = client.get("/api/operations/request-1")

    assert response.status_code == 200
    assert response.json()["status"] == "approved"


def test_api_settings_hive_comms_sessions_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    assert client.get("/api/settings/hive-comms/sessions").status_code == 401


def test_api_settings_hive_comms_sessions_lists_from_gateway(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    fake_sessions = [{"id": "sess-1", "mind_id": "mind-1", "status": "idle"}]
    broker_minds = [{"id": "mind-1", "name": "bob"}]
    with (
        patch.object(main_mod, "_gateway_json", new=AsyncMock(return_value=fake_sessions)),
        patch.object(
            main_mod, "_broker_minds_safe", new=AsyncMock(return_value=broker_minds)
        ),
    ):
        response = client.get("/api/settings/hive-comms/sessions")

    assert response.status_code == 200
    assert response.json()["sessions"] == [
        {**fake_sessions[0], "mind_name": "bob"}
    ]


def test_api_settings_hive_comms_session_delete_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    assert client.delete("/api/settings/hive-comms/sessions/sess-1").status_code == 401


def test_api_settings_hive_comms_session_delete_proxies_to_gateway(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    class _FakeResp:
        status_code = 200
        content = b'{"ok": true}'
        def json(self):
            return {"ok": True}

    class _FakeAsyncClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def delete(self, url, headers=None):
            _FakeAsyncClient.requested_url = url
            return _FakeResp()

    with patch("main.httpx.AsyncClient", _FakeAsyncClient):
        response = client.delete("/api/settings/hive-comms/sessions/sess-1")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert "/sessions/sess-1" in _FakeAsyncClient.requested_url


# ── hive-tools ─────────────────────────────────────────────────────────


def test_api_settings_hive_tools_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    assert client.get("/api/settings/hive-tools").status_code == 401


def test_api_settings_hive_tools_reports_presence_and_health(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    with patch.object(main_mod, "hive_tools_status", return_value={"present": True, "path": "/x"}), \
         patch.object(main_mod, "_service_health", new=AsyncMock(return_value={"reachable": True, "detail": {}})):
        response = client.get("/api/settings/hive-tools")

    assert response.status_code == 200
    body = response.json()
    assert body["present"] is True
    assert body["container_health"]["reachable"] is True
    assert body["remote_configured"] is False


# ── voice ──────────────────────────────────────────────────────────────


def test_api_settings_voice_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    assert client.get("/api/settings/voice").status_code == 401


def test_api_settings_voice_merges_server_health_and_per_mind_refs(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    broker_minds = [{"name": "bob"}, {"name": "ada"}]
    with patch.object(main_mod, "_broker_minds_safe", new=AsyncMock(return_value=broker_minds)), \
         patch.object(main_mod, "_service_health", new=AsyncMock(return_value={"reachable": True, "detail": {}})), \
         patch.object(main_mod, "voice_ref_status", side_effect=lambda n: {"size_bytes": 1} if n == "bob" else None):
        response = client.get("/api/settings/voice")

    assert response.status_code == 200
    body = response.json()
    assert set(body["servers"]) == {"chatterbox", "kokoro"}
    by_name = {m["name"]: m for m in body["minds"]}
    assert by_name["bob"]["voice_ref"]["size_bytes"] == 1
    assert by_name["ada"]["voice_ref"] is None


def test_api_settings_voice_upload_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    response = client.post(
        "/api/settings/voice/bob/voice-ref",
        files={"file": ("ref.wav", b"RIFF....WAVE", "audio/wav")},
    )
    assert response.status_code == 401


def test_api_settings_voice_upload_saves_via_settings_data(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    target = tmp_path / "bob" / "voice_ref.wav"
    with patch.object(main_mod, "save_voice_ref", return_value=target) as mock_save:
        response = client.post(
            "/api/settings/voice/bob/voice-ref",
            files={"file": ("ref.wav", b"RIFF....WAVE", "audio/wav")},
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    mock_save.assert_called_once()
    assert mock_save.call_args[0][0] == "bob"


def test_api_settings_voice_upload_rejects_invalid_wav(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    with patch.object(main_mod, "save_voice_ref", side_effect=ValueError("not a WAV file")):
        response = client.post(
            "/api/settings/voice/bob/voice-ref",
            files={"file": ("ref.wav", b"not audio", "audio/wav")},
        )

    assert response.status_code == 400


# ── downloads ──────────────────────────────────────────────────────────


def test_api_settings_downloads_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    assert client.get("/api/settings/downloads").status_code == 401


def test_api_settings_downloads_lists_from_settings_data(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    fake = [{"filename": "hive-init.py", "size_bytes": 271}]
    with patch.object(main_mod, "list_downloads", return_value=fake):
        response = client.get("/api/settings/downloads")

    assert response.status_code == 200
    assert response.json()["downloads"] == fake


def test_download_asset_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    assert client.get("/downloads/hive-init.py").status_code == 401


def test_download_asset_404s_for_unresolved_filename(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    with patch.object(main_mod, "resolve_download_path", return_value=None):
        response = client.get("/downloads/hive-init.py")
    assert response.status_code == 404


def test_download_asset_serves_resolved_file(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    real_file = tmp_path / "hive-init.py"
    real_file.write_text("print('hi')")
    with patch.object(main_mod, "resolve_download_path", return_value=real_file):
        response = client.get("/downloads/hive-init.py")

    assert response.status_code == 200
    assert response.text == "print('hi')"


# ── new-mind name check ─────────────────────────────────────────────────


def test_api_settings_check_mind_name_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    assert client.get("/api/settings/new-mind/check-name?name=bob").status_code == 401


def test_api_settings_check_mind_name_flags_taken_name(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    with patch.object(main_mod, "_broker_minds_safe", new=AsyncMock(return_value=[{"name": "bob"}])):
        taken = client.get("/api/settings/new-mind/check-name?name=Bob")
        free = client.get("/api/settings/new-mind/check-name?name=newton")

    assert taken.json() == {"name": "bob", "available": False}
    assert free.json() == {"name": "newton", "available": True}


def test_mind_configuration_flags_non_container_deployment(tmp_path, monkeypatch):
    """A systemd edge Mind saves its model but cannot be bounced from here."""
    client = _authed_client(tmp_path, monkeypatch)
    updated = {
        "name": "skippy",
        "provider": "anthropic",
        "deployment": "systemd",
        "default_model": "opus",
    }
    with (
        patch.object(
            main_mod,
            "local_runtime_configuration",
            return_value={"name": "skippy", "default_model": "sonnet"},
        ),
        patch.object(main_mod, "update_local_runtime_model", return_value=updated),
        patch.object(main_mod, "_gateway_request", new=AsyncMock(return_value={})),
    ):
        response = client.patch(
            "/api/minds/skippy/configuration", json={"default_model": "opus"}
        )

    body = response.json()
    assert response.status_code == 200
    assert body["restartable"] is False
    assert body["configuration"]["deployment"] == "systemd"


def test_mind_restart_refuses_non_container_deployment(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    tools = AsyncMock(return_value={})
    with (
        patch.object(
            main_mod,
            "local_runtime_configuration",
            return_value={"name": "skippy", "deployment": "systemd"},
        ),
        patch.object(main_mod, "_hive_tools_request", new=tools),
    ):
        response = client.post("/api/minds/skippy/restart")

    assert response.status_code == 400
    assert "systemd" in response.json()["detail"]
    tools.assert_not_awaited()


# ── credentials ────────────────────────────────────────────────────────


def test_api_settings_credentials_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    assert client.get("/api/settings/credentials").status_code == 401


def test_api_settings_credentials_lists_from_credentials_data(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    fake = [{"key": "comms_bearer_token", "label": "Hive Comms bearer token", "rotatable": True}]
    with patch.object(main_mod, "list_credentials", return_value=fake):
        response = client.get("/api/settings/credentials")

    assert response.status_code == 200
    assert response.json()["credentials"] == fake


def test_api_settings_credentials_rotate_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    response = client.post("/api/settings/credentials/comms_bearer_token/rotate")
    assert response.status_code == 401


def test_api_settings_credentials_rotate_returns_result(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    fake_result = {
        "key": "comms_bearer_token",
        "label": "Hive Comms bearer token",
        "preview": "...abcd",
        "written": [{"file": "nervous_system", "env_key": "COMMS_BEARER_TOKEN"}],
        "restart_targets": ["hive-comms container"],
    }
    with patch.object(main_mod, "rotate_credential", return_value=fake_result) as mock_rotate:
        response = client.post("/api/settings/credentials/comms_bearer_token/rotate")

    assert response.status_code == 200
    assert response.json() == fake_result
    mock_rotate.assert_called_once_with("comms_bearer_token")


def test_api_settings_credentials_rotate_rejects_manual_only(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    with patch.object(
        main_mod, "rotate_credential",
        side_effect=ValueError("Issued per-consumer from hive-tools' own token store"),
    ):
        response = client.post("/api/settings/credentials/hive_tools_token/rotate")

    assert response.status_code == 400
    assert "hive-tools" in response.json()["detail"]
