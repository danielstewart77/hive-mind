"""Pasted-key behavior agreed with Daniel; all writes are disposable.

Run in an isolated test container with a scratch database, no live env/files,
and no Docker socket. HTTP service boundaries are in-memory transports; the
Docker CLI is used only to parse the generated Compose configuration.
"""
import json
import logging
import subprocess

import httpx
import pytest

import configuration_data
import credentials_data
import hive_logging
import holder_credentials

KEY = "test-only-pasted-proxy-key-0123456789"


@pytest.fixture
def creation(tmp_path, monkeypatch, builtin_console):
    minds = tmp_path / "minds"
    minds.mkdir()
    env = tmp_path / ".env"
    env.write_text(f"HOST_PROJECT_DIR={tmp_path}\nUNRELATED=keep\n")
    stack = tmp_path / "docker-compose.yml"
    existing = minds / "existing/container"
    existing.mkdir(parents=True)
    (existing / "compose.yaml").write_text("services: {}\n")
    stack.write_text("networks:\n  hivemind:\n    external: true\n    name: hivemind\ninclude:\n  - path: minds/existing/container/compose.yaml\n")
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_DIR", minds)
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_CONFIG_DIR", minds)
    monkeypatch.setenv("HIVE_MIND_STACK_COMPOSE", str(stack))
    monkeypatch.setenv("HIVE_MIND_PROJECT_PATH", str(tmp_path))
    monkeypatch.setenv("INFERENCE_PROXY_URL", "http://proxy.invalid:8899")
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "fixture-gateway-admin")
    monkeypatch.setenv("LUCENT_ADMIN_BEARER_TOKEN", "fixture-memory-admin")
    monkeypatch.setenv("HIVE_TOOLS_TOKEN", "fixture-tools-token")
    monkeypatch.delenv("PROXY_CREDENTIAL_ADMIN_KEY", raising=False)
    files = {"hive_mind": env}
    monkeypatch.setattr(credentials_data, "ENV_FILES", files)
    monkeypatch.setattr(holder_credentials, "ENV_FILES", files)
    monkeypatch.setattr(credentials_data, "BACKUP_DIR", tmp_path / "backups")
    state = {"proxy_status": 200, "proxy_requests": []}

    def respond(request):
        if request.url.path == "/v1/models":
            state["proxy_requests"].append(request)
            # Deliberately echo the fake secret on refusal: product errors
            # must never relay an upstream response body containing a key.
            return httpx.Response(state["proxy_status"], json={"data": [], "detail": KEY})
        if request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/docker/up":
            return httpx.Response(200, json={"status": "success"})
        return httpx.Response(200, json={"ok": True})

    original = httpx.AsyncClient

    class FixtureClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(respond)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", FixtureClient)
    return builtin_console.admin(), minds, env, stack, state


def body(key, harness="codex_cli", provider="openai"):
    return {"name": "pasted-key-fixture", "harness": harness, "provider": provider,
            "default_model": "fixture-model", "proxy_key": key}


@pytest.mark.parametrize("harness,provider", [("codex_cli", "openai"), ("claude_cli", "anthropic")])
def test_pasted_key_reaches_the_created_mind_without_issuing_another_key(
    creation, caplog, harness, provider,
):
    client, minds, env, stack, state = creation
    response = client.post("/api/minds", json=body(KEY, harness, provider))
    assert response.status_code == 200, response.text
    assert response.json()["credential_error"] is None
    assert KEY not in response.text and KEY not in caplog.text
    assert env.read_text().split("PROXY_TOKEN_PASTED_KEY_FIXTURE=", 1)[1].splitlines()[0] == KEY
    assert state["proxy_requests"][0].headers["authorization"] == f"Bearer {KEY}"
    parsed = subprocess.run(
        ["docker", "compose", "--env-file", str(env), "-f", str(stack), "config", "--format", "json"],
        capture_output=True, text=True,
    )
    assert parsed.returncode == 0, parsed.stderr
    runtime_env = json.loads(parsed.stdout)["services"]["pasted-key-fixture"]["environment"]
    assert runtime_env["OPENAI_API_KEY"] == KEY
    assert runtime_env["ANTHROPIC_AUTH_TOKEN"] == KEY
    assert runtime_env["INFERENCE_PROXY_URL"] == "http://proxy.invalid:8899"


@pytest.mark.parametrize("key", ["bad key", "bad\nINJECTED=value", {"key": "bad"}, "x" * 4097])
def test_malformed_pasted_key_is_refused_before_creation(creation, key):
    client, minds, env, stack, _ = creation
    before = env.read_text(), stack.read_text()
    response = client.post("/api/minds", json=body(key))
    assert response.status_code == 400
    assert not (minds / "pasted-key-fixture").exists()
    assert (env.read_text(), stack.read_text()) == before


@pytest.mark.parametrize("status", [401, 403, 503])
def test_unverified_pasted_key_is_refused_without_writes_or_secret_echo(creation, caplog, status):
    client, minds, env, stack, state = creation
    state["proxy_status"] = status
    before = env.read_text(), stack.read_text()
    response = client.post("/api/minds", json=body(KEY))
    assert response.status_code in (400, 502)
    assert KEY not in response.text and KEY not in caplog.text
    assert not (minds / "pasted-key-fixture").exists()
    assert (env.read_text(), stack.read_text()) == before


def test_structured_logging_hides_a_pasted_proxy_key():
    record = logging.LogRecord("fixture", logging.INFO, "", 0, "mind creation", (), None)
    record.hive_fields = {"proxy_key": KEY}
    assert KEY not in hive_logging.HiveJsonFormatter().format(record)
