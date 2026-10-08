"""Route tests for the consumer-facing Hive control center."""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

import httpx
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


def test_a_console_page_is_never_served_from_cache(tmp_path, monkeypatch):
    """Each page carries its script inline, so a cached page is a cached app —
    and a stale tab's controls silently send nothing."""
    client = _authed_client(tmp_path, monkeypatch)

    for path in ("/minds", "/minds/ada", "/canvas", "/overview"):
        headers = client.get(path).headers
        assert headers["Cache-Control"] == "no-store, must-revalidate", path
        assert headers["Pragma"] == "no-cache", path


def test_configuration_surfaces_render_actionable_controls(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    assert "Save and test" in client.get("/capability-nodes").text
    assert "Verify and register Mind" in client.get("/edge-minds/new").text
    assert "Save model" in client.get("/minds/ada").text


def test_both_kinds_of_mind_are_reachable_from_the_console(tmp_path, monkeypatch):
    """Internal and edge minds are separate install paths, and the console has
    to offer both — /minds only ever linked to the edge form."""
    client = _authed_client(tmp_path, monkeypatch)

    setup = client.get("/setup").text
    minds = client.get("/minds").text

    for page in (setup, minds):
        assert 'href="/minds/new"' in page
        assert 'href="/edge-minds/new"' in page
    # Identity is still conversational; the form scaffolds a runtime only.
    assert "authored in conversation" in setup
    assert "A name check does not reserve the name or install a runtime" in setup


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
        patch.object(
            main_mod,
            "hive_tools_install_status",
            return_value={
                "installed": True,
                "installing": False,
                "path": "/tools",
                "repository": "https://example.invalid/hive-tools.git",
            },
        ),
    ):
        response = client.get("/api/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["counts"] == {
        "minds": 1,
        "edge_minds": 1,
        "capability_nodes": 4,
        "sessions": 1,
    }
    assert body["services"]["comms"]["reachable"] is True
    assert body["services"]["lucent"]["reachable"] is True
    assert body["services"]["tools"]["installed"] is True
    assert body["minds"][0]["last_activity"] == 456
    assert body["minds"][0]["activity_status"] == "active"
    assert body["sessions"][0]["mind_name"] == "ada"
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


def test_session_mind_names_resolve_ids_without_changing_routing_values():
    sessions = [
        {"id": "session-1", "mind_id": "mind-1"},
        {"id": "session-2", "mind_id": "missing-id"},
    ]
    registry = [{"id": "mind-1", "name": "Mordecai"}]

    rows = main_mod._add_session_mind_names(sessions, registry)

    assert rows[0]["mind_name"] == "Mordecai"
    assert rows[0]["mind_id"] == "mind-1"
    assert rows[1]["mind_name"] == "Unknown mind"
    assert "mind_name" not in sessions[0]


def test_unlabelled_mind_identifiers_are_not_rendered_in_console_views():
    templates = main_mod.BASE_DIR / "templates"
    activity = (templates / "activity.html").read_text()
    overview = (templates / "overview.html").read_text()
    settings = (templates / "settings.html").read_text()
    rules = (templates / "rules.html").read_text()
    rosters = "".join(
        (templates / name).read_text()
        for name in ("overview.html", "minds.html", "edge_minds.html")
    )

    assert "session.mind_id" not in activity
    assert "session.mind_id" not in overview
    assert "item.mind_id" not in settings
    assert "m.id[:8]" not in rules
    assert "event-mind-raw" not in rules
    assert 'h.escapeHtml((mind.id || "").slice(0, 8))' not in rosters
    assert 'h.escapeHtml((item.id || "").slice(0, 8))' not in rosters


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


def _bootstrap_body(**overrides):
    body = {
        "name": "atlas",
        "profile": "standard",
        "role": "operator",
        "deployment": "container",
        "harness": "claude_cli",
        "provider": "anthropic",
        "surfaces": ["telegram"],
        "port": 8421,
    }
    body.update(overrides)
    return body


# Harness and provider are separate axes. Every pair here has a working
# mind_templates/ implementation; the console used to offer a single fused
# dropdown that omitted codex+ollama entirely.
_HARNESS_PROVIDER_PAIRS = [
    ("claude_cli", "anthropic", "claude-sonnet-5"),
    ("claude_cli", "ollama", "qwen3:8b"),
    ("codex_cli", "openai", "gpt-5.4"),
    ("codex_cli", "ollama", "qwen3:8b"),
]


@pytest.mark.parametrize("harness,provider,model", _HARNESS_PROVIDER_PAIRS)
@pytest.mark.parametrize("deployment", ["systemd", "container", "windows-task"])
def test_edge_mind_bootstrap_covers_every_harness_provider_deployment(
    tmp_path, monkeypatch, harness, provider, model, deployment
):
    client = _authed_client(tmp_path, monkeypatch)
    monkeypatch.setenv("OLLAMA_URL", "http://192.168.4.64:8899")

    response = client.post(
        "/api/edge-minds/bootstrap",
        json=_bootstrap_body(
            harness=harness, provider=provider, deployment=deployment
        ),
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    command = body["command"]
    # The generated command has to be one setup.sh actually accepts.
    assert f"--harness {harness}" in command
    assert f"--provider {provider}" in command
    assert f"--deployment {deployment}" in command
    assert body["registration"]["harness"] == harness
    assert body["registration"]["provider"] == provider
    assert body["registration"]["model"] == model


def test_edge_mind_bootstrap_emits_the_role_setup_sh_accepts(tmp_path, monkeypatch):
    """setup.sh's roles are operator|sandboxed. The console said `satellite`,
    so every satellite bootstrap it generated died on setup.sh's role check."""
    client = _authed_client(tmp_path, monkeypatch)

    ok = client.post(
        "/api/edge-minds/bootstrap", json=_bootstrap_body(role="sandboxed")
    )
    assert ok.status_code == 200
    assert "--role sandboxed" in ok.json()["command"]

    stale = client.post(
        "/api/edge-minds/bootstrap", json=_bootstrap_body(role="satellite")
    )
    assert stale.status_code == 400


def test_edge_mind_bootstrap_rejects_pairs_the_cli_cannot_serve(
    tmp_path, monkeypatch
):
    client = _authed_client(tmp_path, monkeypatch)

    for harness, provider in [("claude_cli", "openai"), ("codex_cli", "anthropic")]:
        response = client.post(
            "/api/edge-minds/bootstrap",
            json=_bootstrap_body(harness=harness, provider=provider),
        )
        assert response.status_code == 400, f"{harness}+{provider} must be rejected"
        assert provider in response.json()["detail"]

    for field, value in [("harness", "codex_sdk_codex"), ("provider", "bogus")]:
        response = client.post(
            "/api/edge-minds/bootstrap", json=_bootstrap_body(**{field: value})
        )
        assert response.status_code == 400
        assert field in response.json()["detail"].lower()


def test_edge_mind_bootstrap_validates_input_and_never_includes_credentials(
    tmp_path, monkeypatch
):
    client = _authed_client(tmp_path, monkeypatch)
    monkeypatch.setenv("EDGE_COMMS_URL", "http://192.168.4.64:8426")
    monkeypatch.setenv("EDGE_LUCENT_URL", "http://192.168.4.64:8425")

    response = client.post(
        "/api/edge-minds/bootstrap",
        json=_bootstrap_body(harness="codex_cli", provider="openai"),
    )

    assert response.status_code == 200
    body = response.json()
    command = body["command"]
    assert "hive-edge-minds" in command
    assert "atlas" in command
    assert "--profile standard" in command
    assert body["mind_id"] in command
    assert "COMMS_URL=http://192.168.4.64:8426" in command
    assert body["components"][0]["name"] == "Edge Mind runtime"
    assert "token" not in command.lower()
    assert "password" not in command.lower()

    invalid = client.post(
        "/api/edge-minds/bootstrap", json=_bootstrap_body(name="../bad")
    )
    assert invalid.status_code == 400


def test_sentinel_edge_mind_bootstrap_is_rejected_until_bundle_exists(
    tmp_path, monkeypatch
):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/edge-minds/bootstrap", json=_bootstrap_body(profile="sentinel")
    )

    assert response.status_code == 400
    assert "not automated" in response.json()["detail"]
    page = client.get("/edge-minds/new").text
    assert 'name="profile" value="sentinel"' not in page


def test_ollama_provider_requires_a_url_and_rewrites_config(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    monkeypatch.delenv("OLLAMA_URL", raising=False)

    with patch.object(main_mod, "get_capability_endpoint", return_value=""):
        missing = client.post(
            "/api/edge-minds/bootstrap", json=_bootstrap_body(provider="ollama")
        )
    assert missing.status_code == 400
    assert "ollama" in missing.json()["detail"].lower()

    monkeypatch.setenv("OLLAMA_URL", "http://192.168.4.64:8899")
    response = client.post(
        "/api/edge-minds/bootstrap",
        json=_bootstrap_body(provider="ollama", default_model="qwen3:8b"),
    )
    assert response.status_code == 200
    command = response.json()["command"]
    assert "http://192.168.4.64:8899" in command
    assert "config.yaml" in command
    assert response.json()["registration"]["model"] == "qwen3:8b"


def test_edge_mind_bootstrap_preserves_an_empty_surface_selection(
    tmp_path, monkeypatch
):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/edge-minds/bootstrap",
        json=_bootstrap_body(harness="codex_cli", provider="openai", surfaces=[]),
    )

    assert response.status_code == 200
    assert "--surfaces none" in response.json()["command"]


def test_install_options_are_the_forms_single_source_of_truth(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    monkeypatch.setenv("OLLAMA_URL", "http://192.168.4.64:8899")

    options = client.get("/api/minds/install-options").json()

    by_harness = {item["value"]: item["providers"] for item in options["harnesses"]}
    assert sorted(by_harness) == ["claude_cli", "codex_cli"]
    assert sorted(by_harness["claude_cli"]) == ["anthropic", "ollama"]
    assert sorted(by_harness["codex_cli"]) == ["ollama", "openai"]
    assert options["roles"] == ["operator", "sandboxed"]
    assert sorted(options["deployments"]) == ["container", "systemd", "windows-task"]

    # Every advertised pair must actually build a plan.
    for harness, providers in by_harness.items():
        for provider in providers:
            response = client.post(
                "/api/edge-minds/bootstrap",
                json=_bootstrap_body(harness=harness, provider=provider),
            )
            assert response.status_code == 200, (harness, provider)

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
                "harness": "codex_cli",
                "provider": "openai",
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
                "harness": "claude_cli",
                "provider": "anthropic",
                "profile": "standard",
                "role": "sandboxed",
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
                "harness": "claude_cli",
                "provider": "anthropic",
                "profile": "standard",
                "role": "sandboxed",
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
        "total": 6,
        "configured": 6,
        "healthy": 6,
        "attention": 0,
    }
    by_id = {node["id"]: node for node in body["nodes"]}
    assert by_id["hive-tools"]["kind"] == "Action gateway"
    assert by_id["ollama"]["kind"] == "Inference"
    assert by_id["inference-proxy"]["repository"] == "https://github.com/danielstewart77/inference-proxy.git"
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


def test_inference_proxy_install_route_clones_fixed_repository(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    result = {
        "installed": True,
        "path": "/inference_proxy_repo",
        "repository": "https://github.com/danielstewart77/inference-proxy.git",
    }
    with patch.object(main_mod, "clone_inference_proxy", return_value=result) as clone:
        response = client.post("/api/capability-nodes/inference-proxy/install")

    assert response.status_code == 200
    assert response.json() == result
    clone.assert_called_once_with()


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
        reachable = all(
            service not in url for service in ("hive-tools", "inference-proxy")
        )
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
        # Pinned, not inherited from the host: the summary counts depend on
        # whether hive-tools is installed, and inside the Hive container the
        # mounted repo really is there.
        patch.object(
            main_mod,
            "hive_tools_install_status",
            return_value={
                "installed": False,
                "installing": False,
                "path": "/tools",
                "repository": "https://github.com/danielstewart77/hive-tools.git",
            },
        ),
        patch.object(main_mod, "_service_health", new=health),
    ):
        response = client.get("/api/capability-nodes")

    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == {
        "total": 6,
        "configured": 2,
        "healthy": 2,
        "attention": 0,
    }
    by_id = {node["id"]: node for node in body["nodes"]}
    assert by_id["hive-tools"]["health"]["detail"] == "Not configured"
    assert by_id["ollama"]["configured"] is False
    assert by_id["pihole"]["configured"] is False
    assert health.await_count == 4


def test_capability_node_health_probes_run_concurrently(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://ollama:11434")
    monkeypatch.setenv("PIHOLE_URL", "http://pihole")

    async def scenario():
        all_started = asyncio.Event()
        started = 0

        async def probe(url, **kwargs):
            nonlocal started
            started += 1
            if started == 6:
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

    assert asyncio.run(scenario())["summary"]["healthy"] == 6


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


def test_a_kokoro_sample_is_spoken_by_the_engine_and_names_no_mind(
    tmp_path, monkeypatch
):
    """A Kokoro voice belongs to the engine, as the catalogue already does, so
    the route names no Mind and asks the one speech server every Mind on this
    hive reads from."""
    client = _authed_client(tmp_path, monkeypatch)
    monkeypatch.setenv("KOKORO_VOICE_URL", "http://voice-kokoro:8422")

    with patch.object(
        main_mod.mind_runtime, "sample_voice", new=AsyncMock(return_value=b"OggS-sample")
    ) as sample:
        response = client.post("/api/voices/sample", json={"voice": "bm_george"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/ogg"
    assert response.content == b"OggS-sample"
    assert sample.await_args.args == ("http://voice-kokoro:8422", "bm_george", "")


def test_a_chatterbox_sample_is_spoken_by_the_chatterbox_server_for_one_mind(
    tmp_path, monkeypatch
):
    """Test 23: a reference clip lives in one Mind's own directory, so the
    engine decides the server and the Mind decides whose recording."""
    client = _authed_client(tmp_path, monkeypatch)
    monkeypatch.setenv("CHATTERBOX_VOICE_URL", "http://voice-chatterbox:8422")

    with patch.object(
        main_mod.mind_runtime, "sample_voice", new=AsyncMock(return_value=b"OggS-clip")
    ) as sample:
        response = client.post(
            "/api/voices/sample",
            json={
                "voice": "_dramitac_mono_voice_ref.wav",
                "voice_engine": "chatterbox",
                "voice_id": "skippy",
            },
        )

    assert response.status_code == 200
    assert response.content == b"OggS-clip"
    assert sample.await_args.args == (
        "http://voice-chatterbox:8422",
        "_dramitac_mono_voice_ref.wav",
        "skippy",
    )


def test_a_chatterbox_sample_without_a_mind_is_refused(tmp_path, monkeypatch):
    """Nothing could tell the server whose directory to look in, and the
    clip it fell back to would be a sample of the wrong voice."""
    client = _authed_client(tmp_path, monkeypatch)

    with patch.object(main_mod.mind_runtime, "sample_voice", new=AsyncMock()) as sample:
        response = client.post(
            "/api/voices/sample",
            json={"voice": "voice_ref.wav", "voice_engine": "chatterbox"},
        )

    assert response.status_code == 400
    sample.assert_not_awaited()


def test_a_sample_naming_an_engine_that_does_not_exist_is_refused(
    tmp_path, monkeypatch
):
    client = _authed_client(tmp_path, monkeypatch)

    with patch.object(main_mod.mind_runtime, "sample_voice", new=AsyncMock()) as sample:
        response = client.post(
            "/api/voices/sample",
            json={"voice": "bm_george", "voice_engine": "festival"},
        )

    assert response.status_code == 400
    sample.assert_not_awaited()


def test_a_voice_sample_refuses_an_unauthenticated_caller(tmp_path, monkeypatch):
    """Synthesis is GPU work on a route that takes a body from the caller."""
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")

    response = TestClient(main_mod.app).post(
        "/api/voices/sample", json={"voice": "bm_george"}, follow_redirects=False
    )

    assert response.status_code in (401, 303)


def test_a_voice_sample_refuses_a_body_naming_no_voice(tmp_path, monkeypatch):
    """`str(body.get("voice"))` made an absent key the literal string "None",
    which is truthy, matches the name pattern, and reaches the voice server as
    a voice — or lands in a Mind's runtime.yaml on the save route."""
    client = _authed_client(tmp_path, monkeypatch)

    with patch.object(main_mod.mind_runtime, "sample_voice", new=AsyncMock()) as sample:
        absent = client.post("/api/voices/sample", json={})
        null = client.post("/api/voices/sample", json={"voice": None})
        blank = client.post("/api/voices/sample", json={"voice": "   "})

    assert [absent.status_code, null.status_code, blank.status_code] == [400, 400, 400]
    assert sample.await_count == 0


def test_a_voice_sample_refuses_a_body_that_is_not_json(tmp_path, monkeypatch):
    """The caller's mistake answers 400, not a 500 out of the JSON decoder."""
    client = _authed_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/voices/sample",
        content=b"not json at all",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400


def test_the_voice_panel_offers_every_clip_in_the_minds_directory(
    tmp_path, monkeypatch
):
    """Test 18: the other recordings in the directory are the alternatives.

    Before this they were invisible — the panel showed a Kokoro catalogue to a
    Mind that speaks by cloning, and the two takes sitting beside the active
    clip could only be selected by renaming files on the voice server's disk.
    """
    client = _authed_client(tmp_path, monkeypatch)
    clips = [
        {"name": "_dramitac_mono_voice_ref.wav", "size_bytes": 11, "modified_at": 1.0},
        {"name": "voice_ref.wav", "size_bytes": 12, "modified_at": 2.0},
    ]
    with patch.object(
        main_mod, "_mind_gateway_url", new=AsyncMock(return_value="http://skippy:8421")
    ), patch.object(
        main_mod.mind_runtime,
        "fetch_configuration",
        new=AsyncMock(return_value={"voice": "voice_ref.wav"}),
    ), patch.object(
        main_mod.mind_runtime,
        "fetch_voice_catalogue",
        new=AsyncMock(return_value={"catalogue": {"voices": []}}),
    ), patch.object(main_mod, "list_voice_clips", return_value=clips):
        response = client.get("/api/minds/skippy/voice")

    assert response.status_code == 200
    body = response.json()
    assert [clip["name"] for clip in body["clips"]] == [
        "_dramitac_mono_voice_ref.wav",
        "voice_ref.wav",
    ]
    assert body["voice"] == "voice_ref.wav"
    # A Mind whose file names no engine is described as what it actually is.
    assert body["voice_engine"] == "chatterbox"
    assert body["clip_missing"] is False


def test_the_voice_panel_says_so_when_the_named_clip_is_gone(tmp_path, monkeypatch):
    """Test 19: a missing recording and never having picked one differ.

    Under Chatterbox a Mind in this state does not speak at all, and the
    remedy is to pick another clip or re-upload that one — not the remedy for
    a Mind that has simply never been configured.
    """
    client = _authed_client(tmp_path, monkeypatch)
    with patch.object(
        main_mod, "_mind_gateway_url", new=AsyncMock(return_value="http://skippy:8421")
    ), patch.object(
        main_mod.mind_runtime,
        "fetch_configuration",
        new=AsyncMock(
            return_value={"voice": "deleted.wav", "voice_engine": "chatterbox"}
        ),
    ), patch.object(
        main_mod.mind_runtime,
        "fetch_voice_catalogue",
        new=AsyncMock(return_value={"catalogue": {"voices": []}}),
    ), patch.object(
        main_mod, "list_voice_clips", return_value=[{"name": "voice_ref.wav"}]
    ):
        response = client.get("/api/minds/skippy/voice")

    body = response.json()
    assert body["clip_missing"] is True
    assert body["voice"] == "deleted.wav"


def test_a_kokoro_minds_voice_is_not_reported_as_a_missing_clip(
    tmp_path, monkeypatch
):
    """A Kokoro voice is not a file, so no directory can be missing it."""
    client = _authed_client(tmp_path, monkeypatch)
    with patch.object(
        main_mod, "_mind_gateway_url", new=AsyncMock(return_value="http://ada:8421")
    ), patch.object(
        main_mod.mind_runtime,
        "fetch_configuration",
        new=AsyncMock(return_value={"voice": "af_heart", "voice_engine": "kokoro"}),
    ), patch.object(
        main_mod.mind_runtime,
        "fetch_voice_catalogue",
        new=AsyncMock(return_value={"catalogue": {"voices": []}}),
    ), patch.object(main_mod, "list_voice_clips", return_value=[]):
        response = client.get("/api/minds/ada/voice")

    body = response.json()
    assert body["voice_engine"] == "kokoro"
    assert body["clip_missing"] is False


def test_saving_a_voice_writes_the_file_before_the_broker_row(tmp_path, monkeypatch):
    """Test 20: the Mind re-registers from its file on its next start, so a
    row written against a file that never took would be undone anyway."""
    client = _authed_client(tmp_path, monkeypatch)
    order = []

    async def push(gateway_url, voice, engine=""):
        order.append(("file", gateway_url, voice, engine))
        return {"voice": voice, "voice_engine": engine}

    async def gateway(method, path, json_body=None, admin=False):
        order.append(("row", method, path, json_body))
        return {}

    with patch.object(
        main_mod, "_mind_gateway_url", new=AsyncMock(return_value="http://skippy:8421")
    ), patch.object(main_mod.mind_runtime, "push_voice", new=push), patch.object(
        main_mod, "_gateway_request", new=gateway
    ), patch.object(
        main_mod,
        "list_voice_clips",
        return_value=[{"name": "_dramitac_mono_voice_ref.wav"}],
    ):
        response = client.put(
            "/api/minds/skippy/voice",
            json={
                "voice": "_dramitac_mono_voice_ref.wav",
                "voice_engine": "chatterbox",
            },
        )

    assert response.status_code == 200
    assert order[0] == (
        "file",
        "http://skippy:8421",
        "_dramitac_mono_voice_ref.wav",
        "chatterbox",
    )
    assert order[1][0] == "row"
    assert order[1][3] == {
        "voice": "_dramitac_mono_voice_ref.wav",
        "voice_engine": "chatterbox",
    }


def test_saving_a_clip_the_mind_no_longer_has_is_refused(tmp_path, monkeypatch):
    """A clip removed under a loaded page would mute the Mind on save.

    Chatterbox resolves the name against the Mind's own directory and refuses
    when it finds nothing, so a save that took this value would report success
    and leave the Mind silent until somebody noticed.
    """
    client = _authed_client(tmp_path, monkeypatch)

    with patch.object(
        main_mod.mind_runtime, "push_voice", new=AsyncMock()
    ) as push, patch.object(
        main_mod, "list_voice_clips", return_value=[{"name": "voice_ref.wav"}]
    ):
        response = client.put(
            "/api/minds/skippy/voice",
            json={"voice": "deleted.wav", "voice_engine": "chatterbox"},
        )

    assert response.status_code == 409
    assert push.await_count == 0


def test_saving_a_kokoro_voice_is_not_checked_against_the_clip_directory(
    tmp_path, monkeypatch
):
    """A Kokoro voice is not a file, so no directory can be missing it."""
    client = _authed_client(tmp_path, monkeypatch)

    async def push(gateway_url, voice, engine=""):
        return {"voice": voice, "voice_engine": engine}

    with patch.object(
        main_mod, "_mind_gateway_url", new=AsyncMock(return_value="http://ada:8421")
    ), patch.object(main_mod.mind_runtime, "push_voice", new=push), patch.object(
        main_mod, "_gateway_request", new=AsyncMock(return_value={})
    ), patch.object(main_mod, "list_voice_clips", return_value=[]):
        response = client.put(
            "/api/minds/ada/voice",
            json={"voice": "af_heart", "voice_engine": "kokoro"},
        )

    assert response.status_code == 200


def test_uploading_a_clip_whose_name_is_taken_is_refused(tmp_path, monkeypatch):
    """The name a second take gets is the name the active clip already has."""
    client = _authed_client(tmp_path, monkeypatch)

    with patch.object(
        main_mod,
        "save_voice_clip",
        side_effect=main_mod.ClipExists("voice_ref.wav is already one"),
    ):
        response = client.post(
            "/api/minds/skippy/voice/clips",
            files={"file": ("voice_ref.wav", b"RIFF....WAVE", "audio/wav")},
        )

    # Not a 400: nothing is wrong with the request, the name is taken.
    assert response.status_code == 409


def test_saving_a_voice_refuses_an_engine_that_does_not_exist(tmp_path, monkeypatch):
    """A Mind pointed at a server nobody runs goes silent, not noisy."""
    client = _authed_client(tmp_path, monkeypatch)

    with patch.object(main_mod.mind_runtime, "push_voice", new=AsyncMock()) as push:
        response = client.put(
            "/api/minds/skippy/voice",
            json={"voice": "af_heart", "voice_engine": "festival"},
        )

    assert response.status_code == 400
    assert push.await_count == 0


def test_saving_a_voice_refuses_a_body_naming_no_voice(tmp_path, monkeypatch):
    """The same idiom on the save route wrote `voice: None` into runtime.yaml."""
    client = _authed_client(tmp_path, monkeypatch)

    with patch.object(main_mod.mind_runtime, "push_voice", new=AsyncMock()) as push:
        response = client.put("/api/minds/skippy/voice", json={"voice": None})

    assert response.status_code == 400
    assert push.await_count == 0
