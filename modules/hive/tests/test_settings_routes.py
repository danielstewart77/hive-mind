"""Route tests for the /settings page and its API endpoints."""

import os
import sys
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi import HTTPException

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


def test_settings_page_redirects_when_unauthenticated(tmp_path, monkeypatch):
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


def test_mind_configuration_route_pushes_to_the_mind_then_the_broker(
    tmp_path, monkeypatch
):
    """The Mind's own runtime.yaml is written first, its broker row second.

    The Mind re-registers from that file on its next start, so a broker row
    written against a file that never took would be silently undone.
    """
    client = _authed_client(tmp_path, monkeypatch)
    gateway = AsyncMock(return_value={"status": "updated"})
    push = AsyncMock(
        return_value={"name": "bob", "provider": "ollama", "default_model": "qwen3:8b"}
    )
    with (
        patch.object(main_mod, "_mind_gateway_url", new=AsyncMock(return_value="http://bob:8420")),
        patch.object(
            main_mod.mind_runtime,
            "fetch_configuration",
            new=AsyncMock(return_value={"name": "bob", "default_model": "old-model"}),
        ),
        patch.object(main_mod.mind_runtime, "push_default_model", new=push),
        patch.object(main_mod, "_gateway_request", new=gateway),
    ):
        response = client.patch(
            "/api/minds/bob/configuration",
            json={"default_model": "qwen3:8b", "provider": "ollama"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["configuration"]["default_model"] == "qwen3:8b"
    # Live at the next session — nothing reads the model at boot.
    assert body["restart_required"] is False
    # Provider and model in one write: a Mind holding a provider that does not
    # host its model is a Mind that stops answering.
    assert push.await_args.args == ("http://bob:8420", "qwen3:8b", "ollama")
    assert gateway.await_args.args[:2] == ("PUT", "/broker/minds/bob")
    assert gateway.await_args.kwargs["json_body"] == {"model": "qwen3:8b"}
    # Broker writes are admin-only; the service token comms hands out for
    # reads is rejected on this route.
    assert gateway.await_args.kwargs["admin"] is True


def test_mind_configuration_reads_the_mind_over_http(tmp_path, monkeypatch):
    """Including a Mind on another machine, which no bind mount can reach."""
    client = _authed_client(tmp_path, monkeypatch)
    models = [
        {"name": "claude-sonnet-5", "label": "Sonnet 5", "provider": "anthropic",
         "provider_label": "Anthropic"},
        {"name": "qwen3:8b", "label": "qwen3:8b", "provider": "ollama",
         "provider_label": "Ollama"},
    ]
    with (
        patch.object(main_mod, "_mind_gateway_url", new=AsyncMock(return_value="http://bob:8420")),
        patch.object(
            main_mod.mind_runtime,
            "fetch_configuration",
            new=AsyncMock(return_value={
                "name": "bob",
                "harness": "claude_cli",
                "provider": "ollama",
                "default_model": "qwen3:8b",
            }),
        ),
        patch.object(
            main_mod.mind_runtime, "fetch_models", new=AsyncMock(return_value=models)
        ),
    ):
        response = client.get("/api/minds/bob/configuration")

    assert response.status_code == 200
    # Relayed as reported: the Mind's own key and harness already decided the
    # list, and each row names the provider the console groups it under.
    assert [row["name"] for row in response.json()["models"]] == [
        "claude-sonnet-5",
        "qwen3:8b",
    ]
    assert response.json()["models"][0]["provider_label"] == "Anthropic"
    assert response.json()["configuration"]["default_model"] == "qwen3:8b"


def test_mind_configuration_restores_the_file_when_the_broker_update_fails(
    tmp_path, monkeypatch
):
    client = _authed_client(tmp_path, monkeypatch)
    push = AsyncMock(
        side_effect=[
            {"name": "bob", "default_model": "qwen3:8b"},
            {"name": "bob", "default_model": "old-model"},
        ]
    )
    with (
        patch.object(main_mod, "_mind_gateway_url", new=AsyncMock(return_value="http://bob:8420")),
        patch.object(
            main_mod.mind_runtime,
            "fetch_configuration",
            new=AsyncMock(return_value={"name": "bob", "default_model": "old-model"}),
        ),
        patch.object(main_mod.mind_runtime, "push_default_model", new=push),
        patch.object(
            main_mod,
            "_gateway_request",
            new=AsyncMock(side_effect=HTTPException(status_code=502, detail="down")),
        ),
    ):
        response = client.patch(
            "/api/minds/bob/configuration", json={"default_model": "qwen3:8b"}
        )

    assert response.status_code == 502
    assert push.await_args_list[1].args == ("http://bob:8420", "old-model", "")


def test_mind_configuration_reports_a_mind_that_is_down(tmp_path, monkeypatch):
    """A Mind that cannot be reached cannot be configured — the file is
    authoritative, so a broker row written alone would not survive its next
    start."""
    client = _authed_client(tmp_path, monkeypatch)
    gateway = AsyncMock(return_value={})
    with (
        patch.object(main_mod, "_mind_gateway_url", new=AsyncMock(return_value="http://bob:8420")),
        patch.object(
            main_mod.mind_runtime,
            "fetch_configuration",
            new=AsyncMock(side_effect=HTTPException(status_code=502, detail="Mind is unreachable")),
        ),
        patch.object(main_mod, "_gateway_request", new=gateway),
    ):
        response = client.patch(
            "/api/minds/bob/configuration", json={"default_model": "qwen3:8b"}
        )

    assert response.status_code == 502
    gateway.assert_not_awaited()


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
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    assert client.get("/api/settings/hive-tools").status_code == 401


def test_api_settings_hive_tools_reports_presence_and_health(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    # remote_configured is a live check of the mounted repo, so it is stubbed
    # rather than left to whether /hive_tools_repo happens to exist on the
    # machine running the tests — inside the Hive container it does.
    with patch.object(main_mod, "hive_tools_status", return_value={"present": True, "path": "/x"}), \
         patch.object(main_mod, "hive_tools_install_status", return_value={"installed": False}), \
         patch.object(main_mod, "_service_health", new=AsyncMock(return_value={"reachable": True, "detail": {}})):
        response = client.get("/api/settings/hive-tools")

    assert response.status_code == 200
    body = response.json()
    assert body["present"] is True
    assert body["container_health"]["reachable"] is True
    assert body["remote_configured"] is False


def test_api_settings_hive_tools_reports_an_installed_repo_as_configured(
    tmp_path, monkeypatch
):
    client = _authed_client(tmp_path, monkeypatch)
    with patch.object(main_mod, "hive_tools_status", return_value={"present": True, "path": "/x"}), \
         patch.object(main_mod, "hive_tools_install_status", return_value={"installed": True}), \
         patch.object(main_mod, "_service_health", new=AsyncMock(return_value={"reachable": True, "detail": {}})):
        response = client.get("/api/settings/hive-tools")

    assert response.json()["remote_configured"] is True


# ── voice ──────────────────────────────────────────────────────────────


def test_api_settings_voice_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    assert client.get("/api/settings/voice").status_code == 401


def test_api_settings_voice_reports_each_server_health(tmp_path, monkeypatch):
    """The tab is about the servers. Per-mind clips live on the Mind's page."""
    client = _authed_client(tmp_path, monkeypatch)
    with patch.object(
        main_mod,
        "_service_health",
        new=AsyncMock(return_value={"reachable": True, "detail": {}}),
    ):
        response = client.get("/api/settings/voice")

    assert response.status_code == 200
    body = response.json()
    assert set(body["servers"]) == {"chatterbox", "kokoro"}
    assert body["servers"]["kokoro"]["reachable"] is True


def test_api_mind_voice_clip_upload_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    response = client.post(
        "/api/minds/bob/voice/clips",
        files={"file": ("ref.wav", b"RIFF....WAVE", "audio/wav")},
    )
    assert response.status_code == 401


def test_api_mind_voice_clip_upload_saves_under_the_sent_filename(
    tmp_path, monkeypatch
):
    client = _authed_client(tmp_path, monkeypatch)
    target = tmp_path / "bob" / "calm_take.wav"
    with patch.object(main_mod, "save_voice_clip", return_value=target) as mock_save, \
         patch.object(main_mod, "list_voice_clips", return_value=[{"name": "calm_take.wav"}]):
        response = client.post(
            "/api/minds/bob/voice/clips",
            files={"file": ("calm_take.wav", b"RIFF....WAVE", "audio/wav")},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["clip"] == "calm_take.wav"
    # The refreshed listing rides back, so the picker gains the new clip
    # without a second request.
    assert body["clips"] == [{"name": "calm_take.wav"}]
    assert mock_save.call_args[0][0] == "bob"
    assert mock_save.call_args[0][1] == "calm_take.wav"


def test_api_mind_voice_clip_upload_rejects_invalid_wav(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    with patch.object(
        main_mod, "save_voice_clip", side_effect=ValueError("not a WAV file")
    ):
        response = client.post(
            "/api/minds/bob/voice/clips",
            files={"file": ("ref.wav", b"not audio", "audio/wav")},
        )

    assert response.status_code == 400


# ── downloads ──────────────────────────────────────────────────────────


def test_api_settings_downloads_requires_auth(tmp_path, monkeypatch):
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
        patch.object(main_mod, "_mind_gateway_url", new=AsyncMock(return_value="http://skippy:8421")),
        patch.object(
            main_mod.mind_runtime,
            "fetch_configuration",
            new=AsyncMock(return_value={"name": "skippy", "default_model": "sonnet"}),
        ),
        patch.object(
            main_mod.mind_runtime, "push_default_model", new=AsyncMock(return_value=updated)
        ),
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
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    assert client.get("/api/settings/credentials").status_code == 401


def test_api_settings_credentials_rotate_requires_auth(tmp_path, monkeypatch):
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
        "locations": [
            {
                "file": "nervous_system",
                "path": "/env/nervous_system.env",
                "env_key": "COMMS_BEARER_TOKEN",
                "written": True,
                "reason": None,
            },
            {
                "file": "hive_tools",
                "path": "/env_hive_tools/.env",
                "env_key": "COMMS_BEARER_TOKEN",
                "written": False,
                "reason": "/env_hive_tools/.env is a directory, not a file",
            },
        ],
        "written": [{"file": "nervous_system", "env_key": "COMMS_BEARER_TOKEN"}],
        "complete": False,
        "restart_targets": ["hive-comms container"],
    }
    with patch.object(main_mod, "rotate_credential", return_value=fake_result) as mock_rotate:
        response = client.post("/api/settings/credentials/comms_bearer_token/rotate")

    assert response.status_code == 200
    assert response.json() == fake_result
    mock_rotate.assert_called_once_with("comms_bearer_token")


def test_api_settings_credentials_set_github_pat_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    response = client.post(
        "/api/settings/credentials/github_pat/set", json={"token": "ghp_x"}
    )
    assert response.status_code == 401


def test_api_settings_credentials_set_github_pat_returns_result(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    fake_result = {
        "key": "github_pat",
        "label": "GitHub personal access token",
        "login": "danielstewart77",
        "preview": "...cdef",
        "locations": [
            {
                "file": "git_credentials",
                "path": "/root/.git-credentials",
                "env_key": "github.com",
                "written": True,
                "reason": None,
            },
            {
                "file": "gh_hosts",
                "path": "/root/.config/gh/hosts.yml",
                "env_key": "oauth_token",
                "written": False,
                "reason": "no file at /root/.config/gh/hosts.yml",
            },
        ],
        "written": [{"file": "git_credentials", "env_key": "github.com"}],
        "complete": False,
        "restart_targets": [],
    }
    with patch.object(main_mod, "set_github_pat", return_value=fake_result) as mock_set:
        response = client.post(
            "/api/settings/credentials/github_pat/set", json={"token": "ghp_new"}
        )

    assert response.status_code == 200
    assert response.json() == fake_result
    mock_set.assert_called_once_with("ghp_new")


def test_api_settings_credentials_set_github_pat_400s_on_rejected_token(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    with patch.object(
        main_mod, "set_github_pat",
        side_effect=ValueError("GitHub rejected that token (401) — nothing was written"),
    ):
        response = client.post(
            "/api/settings/credentials/github_pat/set", json={"token": "ghp_bad"}
        )

    assert response.status_code == 400
    assert "nothing was written" in response.json()["detail"]


def test_api_settings_credentials_rotate_rejects_manual_only(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    with patch.object(
        main_mod, "rotate_credential",
        side_effect=ValueError("Issued per-consumer from hive-tools' own token store"),
    ):
        response = client.post("/api/settings/credentials/hive_tools_token/rotate")

    assert response.status_code == 400
    assert "hive-tools" in response.json()["detail"]
