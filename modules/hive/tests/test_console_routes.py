"""Route tests for the consumer-facing Hive control center."""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

import httpx
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


def test_login_uses_edge_mind_taxonomy(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    response = TestClient(main_mod.app).get("/login")

    assert response.status_code == 200
    assert "Edge Minds" in response.text
    assert "Edge Nodes" not in response.text


def test_console_pages_require_authentication(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    for path in (
        "/overview",
        "/setup",
        "/minds",
        "/edge-minds",
        "/capability-nodes",
        "/activity",
    ):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login?next="), path


def test_console_pages_render_the_new_application_shell(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    for path, marker in (
        ("/overview", "System overview"),
        ("/setup", "Set up your Hive"),
        ("/minds", "Minds"),
        ("/edge-minds", "Edge Minds"),
        ("/capability-nodes", "Capability Nodes"),
        ("/activity", "Activity"),
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert marker in response.text, path
        assert "console-sidebar" in response.text, path


def test_configuration_surfaces_render_actionable_controls(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    assert "Save and test" in client.get("/capability-nodes").text
    assert "Verify and register Mind" in client.get("/edge-minds/new").text
    assert "Request restart" in client.get("/minds/ada").text


def test_core_mind_installation_is_not_presented_as_automated(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    setup = client.get("/setup").text
    minds = client.get("/minds").text

    assert "Core Mind provisioning is not automated yet" in setup
    assert "A name check does not reserve the name or install a runtime" in setup
    assert "Add an Edge Mind" in setup
    assert "Add Edge Mind" in minds
    assert ">Create mind<" not in minds


def test_new_configuration_mutations_require_authentication(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    requests = (
        client.put(
            "/api/capability-nodes/ollama/configuration",
            json={"endpoint": "http://ollama:11434"},
        ),
        client.post(
            "/api/capability-nodes/ollama/models/pull",
            json={"model": "qwen3:8b"},
        ),
        client.patch(
            "/api/minds/bob/configuration", json={"default_model": "qwen3:8b"}
        ),
        client.post("/api/minds/bob/restart"),
        client.post("/api/edge-minds/verify", json={}),
    )

    assert all(response.status_code == 401 for response in requests)


def test_legacy_topology_pages_redirect_to_new_names(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    for old_path, new_path in (
        ("/outposts", "/edge-minds"),
        ("/outposts/new", "/edge-minds/new"),
        ("/tools", "/capability-nodes"),
    ):
        response = client.get(old_path, follow_redirects=False)
        assert response.status_code == 308
        assert response.headers["location"] == new_path


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
    assert "new URL(mind.gateway_url)" in source
    assert "normalizeOverview: normalizeOverview" in source
    assert "Diagnostic" in source


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
    minds = [
        {
            "name": "ada",
            "id": "mind-1",
            "gateway_url": "http://192.168.4.20:8421",
            "last_seen": 123,
        }
    ]
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
    assert body["counts"] == {
        "minds": 1,
        "edge_minds": 1,
        "capability_nodes": 3,
        "sessions": 1,
    }
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


def test_edge_minds_api_returns_external_minds_as_managed_nodes(tmp_path, monkeypatch):
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
        response = client.get("/api/edge-minds")

    assert response.status_code == 200
    assert [row["name"] for row in response.json()["edge_minds"]] == ["arnold"]
    assert response.json()["edge_minds"][0]["host"] == "deadpool"
    assert response.json()["edge_minds"][0]["profile"] == "standard"


def test_edge_mind_rows_require_topology_evidence_and_apply_profile_overrides(
    monkeypatch,
):
    monkeypatch.setenv("EDGE_MIND_PROFILES", "hex:sentinel")
    registry = [
        {
            "name": "ada",
            "id": "local-1",
            "gateway_url": "http://ada:8420",
            "local_config": None,
        },
        {
            "name": "hex",
            "id": "edge-1",
            "gateway_url": "http://192.168.4.16:8421",
            "local_config": None,
        },
    ]

    rows = main_mod._edge_mind_rows(registry)

    assert [row["name"] for row in rows] == ["hex"]
    assert rows[0]["profile"] == "sentinel"


def test_edge_mind_bootstrap_validates_input_and_never_includes_credentials(
    tmp_path, monkeypatch
):
    client = _authed_client(tmp_path, monkeypatch)
    monkeypatch.setenv("EDGE_COMMS_URL", "http://192.168.4.64:8426")
    monkeypatch.setenv("EDGE_LUCENT_URL", "http://192.168.4.64:8425")

    response = client.post(
        "/api/edge-minds/bootstrap",
        json={
            "name": "atlas",
            "profile": "standard",
            "role": "satellite",
            "deployment": "container",
            "harness": "codex_cli_codex",
            "surfaces": ["telegram"],
            "port": 8421,
        },
    )

    assert response.status_code == 200
    body = response.json()
    command = body["command"]
    assert "hive-edge-minds" in command
    assert "atlas" in command
    assert "--profile standard" in command
    assert body["mind_id"] in command
    assert "COMMS_URL=http://192.168.4.64:8426" in command
    assert body["registration"]["name"] == "atlas"
    assert body["components"][0]["name"] == "Edge Mind runtime"
    assert "token" not in command.lower()
    assert "password" not in command.lower()

    invalid = client.post(
        "/api/edge-minds/bootstrap",
        json={
            "name": "../bad",
            "profile": "standard",
            "role": "operator",
            "deployment": "systemd",
            "harness": "claude_cli_claude",
            "surfaces": [],
            "port": 8421,
        },
    )
    assert invalid.status_code == 400


def test_sentinel_edge_mind_bootstrap_is_rejected_until_bundle_exists(
    tmp_path, monkeypatch
):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/edge-minds/bootstrap",
        json={
            "name": "sentinel-one",
            "profile": "sentinel",
            "role": "operator",
            "deployment": "container",
            "harness": "codex_cli_codex",
            "surfaces": ["telegram"],
            "port": 8421,
        },
    )

    assert response.status_code == 400
    assert "not automated" in response.json()["detail"]
    page = client.get("/edge-minds/new").text
    assert 'name="profile" value="sentinel"' not in page
    assert "installing the full Sentinel" in page


def test_edge_mind_bootstrap_wires_supported_ollama_harness(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/edge-minds/bootstrap",
        json={
            "name": "local-one",
            "profile": "standard",
            "role": "operator",
            "deployment": "container",
            "harness": "claude_cli_ollama",
            "surfaces": ["telegram"],
            "port": 8421,
            "default_model": "qwen3:8b",
            "ollama_url": "http://192.168.4.64:11434",
        },
    )

    assert response.status_code == 200
    command = response.json()["command"]
    assert "http://192.168.4.64:11434" in command
    assert "config.yaml" in command
    assert response.json()["registration"]["model"] == "qwen3:8b"


def test_edge_mind_bootstrap_preserves_an_empty_surface_selection(
    tmp_path, monkeypatch
):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/edge-minds/bootstrap",
        json={
            "name": "headless-one",
            "profile": "standard",
            "role": "satellite",
            "deployment": "container",
            "harness": "codex_cli_codex",
            "surfaces": [],
            "port": 8421,
        },
    )

    assert response.status_code == 200
    assert "--surfaces none" in response.json()["command"]


def test_edge_mind_bootstrap_rejects_unimplemented_harness(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/edge-minds/bootstrap",
        json={
            "name": "future-one",
            "profile": "standard",
            "role": "satellite",
            "deployment": "container",
            "harness": "codex_sdk_codex",
            "surfaces": [],
            "port": 8421,
        },
    )

    assert response.status_code == 400
    assert "harness" in response.json()["detail"].lower()


def test_edge_mind_verify_checks_health_and_registers_with_comms(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    mind_id = "6f12a60a-b5fd-4ba4-b2f1-07065a49145d"
    gateway_request = AsyncMock(return_value={"status": "registered"})
    metadata = patch.object(main_mod, "set_edge_mind_metadata")
    with (
        patch.object(
            main_mod,
            "_probe_edge_mind",
            new=AsyncMock(return_value={"mind_id": mind_id, "status": "ok"}),
        ),
        patch.object(main_mod, "_gateway_request", new=gateway_request),
        metadata as metadata_mock,
    ):
        response = client.post(
            "/api/edge-minds/verify",
            json={
                "name": "sentinel-one",
                "mind_id": mind_id,
                "gateway_url": "http://192.168.4.16:8421",
                "model": "gpt-5.5",
                "harness": "codex_cli_codex",
                "profile": "sentinel",
                "role": "operator",
                "deployment": "container",
            },
        )

    assert response.status_code == 200
    assert response.json()["registered"] is True
    assert gateway_request.await_args.args[:2] == ("POST", "/broker/minds")
    assert gateway_request.await_args.kwargs["json_body"]["mind_id"] == mind_id
    metadata_mock.assert_called_once_with(
        "sentinel-one",
        profile="sentinel",
        role="operator",
        deployment="container",
    )


def test_edge_mind_verify_rejects_mismatched_identity(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    with patch.object(
        main_mod,
        "_probe_edge_mind",
        new=AsyncMock(return_value={"mind_id": "different", "status": "ok"}),
    ):
        response = client.post(
            "/api/edge-minds/verify",
            json={
                "name": "atlas",
                "mind_id": "6f12a60a-b5fd-4ba4-b2f1-07065a49145d",
                "gateway_url": "http://192.168.4.20:8421",
                "model": "sonnet",
                "harness": "claude_cli_claude",
                "profile": "standard",
                "role": "satellite",
                "deployment": "systemd",
            },
        )

    assert response.status_code == 400
    assert "identity" in response.json()["detail"].lower()


def test_edge_mind_verify_rejects_an_unready_runtime(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    mind_id = "6f12a60a-b5fd-4ba4-b2f1-07065a49145d"
    with patch.object(
        main_mod,
        "_probe_edge_mind",
        new=AsyncMock(return_value={"mind_id": mind_id, "status": "degraded"}),
    ):
        response = client.post(
            "/api/edge-minds/verify",
            json={
                "name": "atlas",
                "mind_id": mind_id,
                "gateway_url": "http://192.168.4.20:8421",
                "model": "sonnet",
                "harness": "claude_cli_claude",
                "profile": "standard",
                "role": "satellite",
                "deployment": "systemd",
            },
        )

    assert response.status_code == 502
    assert "not ready" in response.json()["detail"].lower()


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


def test_capability_nodes_api_groups_external_services_by_function(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    monkeypatch.setenv("OLLAMA_URL", "http://ollama:11434")
    monkeypatch.setenv("PIHOLE_URL", "http://pihole")
    status = {
        "present": True,
        "path": "/tools",
        "capabilities": [{"id": "gmail", "name": "Gmail"}],
    }
    with (
        patch.object(main_mod, "hive_tools_status", return_value=status),
        patch.object(
            main_mod,
            "_service_health",
            new=AsyncMock(
                return_value={
                    "reachable": True,
                    "message": "Ready",
                    "action": "No action is required.",
                }
            ),
        ),
    ):
        response = client.get("/api/capability-nodes")

    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == {
        "total": 5,
        "configured": 5,
        "healthy": 5,
        "attention": 0,
    }
    by_id = {node["id"]: node for node in body["nodes"]}
    assert by_id["hive-tools"]["kind"] == "Action gateway"
    assert by_id["ollama"]["kind"] == "Inference"
    assert by_id["pihole"]["kind"] == "Network service"
    assert by_id["hive-tools"]["capabilities"][0]["name"] == "Gmail"


def test_capability_node_configuration_route_saves_endpoint(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    configured = {
        "summary": {"total": 1, "configured": 1, "healthy": 1, "attention": 0},
        "nodes": [{"id": "ollama", "configured": True, "endpoint": "http://ollama:11434"}],
    }
    with (
        patch.object(
            main_mod,
            "set_capability_endpoint",
            return_value="http://ollama:11434",
        ) as save,
        patch.object(
            main_mod,
            "_capability_node_data",
            new=AsyncMock(return_value=configured),
        ),
    ):
        response = client.put(
            "/api/capability-nodes/ollama/configuration",
            json={"endpoint": "http://ollama:11434"},
        )

    assert response.status_code == 200
    assert response.json()["node"]["configured"] is True
    save.assert_called_once_with("ollama", "http://ollama:11434")


def test_ollama_models_api_lists_installed_and_running_models(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    responses = {
        "/api/version": {"version": "0.30.8"},
        "/api/tags": {"models": [{"name": "qwen3:8b"}]},
        "/api/ps": {"models": [{"name": "qwen3-embedding:8b"}]},
    }

    async def request(method, path, **kwargs):
        assert method == "GET"
        return responses[path]

    with patch.object(main_mod, "_ollama_request", new=request):
        response = client.get("/api/capability-nodes/ollama/models")

    assert response.status_code == 200
    assert response.json()["version"] == "0.30.8"
    assert response.json()["models"][0]["name"] == "qwen3:8b"
    assert response.json()["running"][0]["name"] == "qwen3-embedding:8b"


def test_ollama_model_pull_and_delete_proxy_validated_requests(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    request = AsyncMock(return_value={"status": "success"})
    with patch.object(main_mod, "_ollama_request", new=request):
        pull = client.post(
            "/api/capability-nodes/ollama/models/pull",
            json={"model": "qwen3:8b"},
        )
        delete = client.request(
            "DELETE",
            "/api/capability-nodes/ollama/models",
            json={"model": "qwen3:8b"},
        )

    assert pull.status_code == 200
    assert delete.status_code == 200
    assert request.await_args_list[0].args == ("POST", "/api/pull")
    assert request.await_args_list[0].kwargs["json_body"] == {
        "model": "qwen3:8b",
        "stream": False,
    }
    assert request.await_args_list[1].args == ("DELETE", "/api/delete")


def test_pihole_unauthorized_probe_is_reachable_but_not_ready():
    response = httpx.Response(
        401,
        json={"error": {"key": "unauthorized"}},
        request=httpx.Request("GET", "http://pihole:8080/api/auth"),
    )

    class StubClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def get(self, url, headers):
            return response

    with patch.object(main_mod.httpx, "AsyncClient", return_value=StubClient()):
        health = asyncio.run(
            main_mod._service_health(
                "http://pihole:8080/api/auth",
                service_name="Pi-hole",
                accepted_statuses={401},
                authentication_required_statuses={401},
            )
        )

    assert health["reachable"] is True
    assert health["ready"] is False
    assert health["authentication_required"] is True


def test_capability_nodes_leave_optional_unconfigured_services_idle(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    for name in ("HIVE_TOOLS_URL", "OLLAMA_URL", "OLLAMA_BASE_URL", "PIHOLE_URL"):
        monkeypatch.delenv(name, raising=False)
    async def probe(url, **kwargs):
        reachable = "hive-tools" not in url
        return {
            "reachable": reachable,
            "message": "Ready" if reachable else "Unavailable",
            "action": "No action is required." if reachable else "Check the service.",
        }

    health = AsyncMock(side_effect=probe)
    with (
        patch.object(
            main_mod,
            "hive_tools_status",
            return_value={"present": False, "path": "/tools", "capabilities": []},
        ),
        patch.object(main_mod, "_service_health", new=health),
    ):
        response = client.get("/api/capability-nodes")

    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == {
        "total": 5,
        "configured": 2,
        "healthy": 2,
        "attention": 0,
    }
    by_id = {node["id"]: node for node in body["nodes"]}
    assert by_id["hive-tools"]["health"]["detail"] == "Not configured"
    assert by_id["ollama"]["configured"] is False
    assert by_id["pihole"]["configured"] is False
    assert health.await_count == 3


def test_capability_node_health_probes_run_concurrently(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://ollama:11434")
    monkeypatch.setenv("PIHOLE_URL", "http://pihole")

    async def scenario():
        all_started = asyncio.Event()
        started = 0

        async def probe(url, **kwargs):
            nonlocal started
            started += 1
            if started == 5:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=1.0)
            return {
                "reachable": True,
                "endpoint": url,
                "message": f"{kwargs['service_name']} ready",
                "action": "No action is required.",
            }

        with (
            patch.object(
                main_mod,
                "hive_tools_status",
                return_value={"present": True, "path": "/tools", "capabilities": []},
            ),
            patch.object(main_mod, "_service_health", new=probe),
        ):
            return await main_mod._capability_node_data()

    assert asyncio.run(scenario())["summary"]["healthy"] == 5


def test_service_health_accepts_successful_non_json_response():
    response = httpx.Response(
        200,
        text="pong",
        headers={"content-type": "text/plain"},
        request=httpx.Request("GET", "http://service/health"),
    )

    class StubClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def get(self, url, headers):
            return response

    with patch.object(main_mod.httpx, "AsyncClient", return_value=StubClient()):
        health = asyncio.run(
            main_mod._service_health(
                "http://service/health",
                service_name="Test service",
            )
        )

    assert health["reachable"] is True
    assert health["endpoint"] == "http://service/health"
    assert health["detail"] == {
        "content_type": "text/plain",
        "response": "pong",
    }
