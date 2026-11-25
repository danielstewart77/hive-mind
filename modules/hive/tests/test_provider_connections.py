"""P1/P2 agreed by Daniel: preserve connections and use each Mind's token."""
import json

import httpx
import pytest
import yaml

import configuration_data
import provider_data
import credentials_data
import holder_credentials
from test_pasted_proxy_key import creation  # noqa: F401 - pytest fixture


async def test_migration_preserves_existing_mind_configuration_and_keys(tmp_path, monkeypatch):
    minds = tmp_path / "minds"
    own = minds / "smith"
    (own / "container").mkdir(parents=True)
    runtime = own / "runtime.yaml"
    runtime.write_text("name: smith\nprovider: proxy\ndefault_model: smith-model\n")
    compose = own / "container/compose.yaml"
    compose.write_text("services:\n  smith:\n    environment:\n      - INFERENCE_PROXY_URL=http://existing:8888\n      - OPENAI_API_KEY=${PROXY_TOKEN_SMITH}\n")
    before = runtime.read_bytes(), compose.read_bytes()
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_CONFIG_DIR", minds)
    monkeypatch.setenv("HIVE_MIND_PROXY_URL", "http://existing:8888")
    providers = await provider_data.list_connections()
    assigned = await provider_data.connection_for_mind("smith")
    assert assigned["base_url"] == "http://existing:8888"
    assert assigned["id"] in {p["id"] for p in providers}
    assert (runtime.read_bytes(), compose.read_bytes()) == before
    assert await provider_data.list_connections() == providers


@pytest.mark.parametrize("kind,path", [("proxy", "/v1/models"), ("openai", "/v1/models"), ("anthropic", "/v1/models"), ("ollama", "/api/tags")])
async def test_selected_connection_enumerates_models_with_minds_token(monkeypatch, kind, path):
    saved = await provider_data.save_connection({"name": "Chosen", "kind": kind, "base_url": "http://selected.invalid"})
    original = httpx.AsyncClient

    def serve(request):
        token = request.headers.get("x-api-key") if kind == "anthropic" else request.headers.get("authorization", "").removeprefix("Bearer ")
        if request.url.host != "selected.invalid" or request.url.path != path or token != "mind-owned-key":
            return httpx.Response(403)
        rows = {"models": [{"name": "chosen-model"}]} if kind == "ollama" else {"data": [{"id": "chosen-model", "context_window": 12345}]}
        return httpx.Response(200, json=rows)

    class Client(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, transport=httpx.MockTransport(serve), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    rows = await provider_data.fetch_models(saved, "mind-owned-key", "dsh")
    assert [row["id"] for row in rows] == ["chosen-model"]
    assert "mind-owned-key" not in json.dumps(await provider_data.list_connections())


def test_scaffold_routes_inference_to_selected_connection_without_embedding_key(tmp_path, monkeypatch):
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_CONFIG_DIR", tmp_path / "minds")
    monkeypatch.setenv("HIVE_MIND_UID", "1000")
    connection = {"id": "direct", "name": "Direct OpenAI", "kind": "openai", "base_url": "https://direct.invalid/v1", "api_format": "openai"}
    configuration_data.scaffold_internal_mind("new-mind", mind_id="00000000-0000-0000-0000-000000000001", harness="dsh", provider="openai", default_model="model-a", owner="daniel", connection=connection)
    compose = yaml.safe_load((tmp_path / "minds/new-mind/container/compose.yaml").read_text())
    env = dict(item.split("=", 1) for item in compose["services"]["new-mind"]["environment"])
    assert env["OPENAI_BASE_URL"] == "https://direct.invalid/v1"
    assert env["OPENAI_API_KEY"] == "${MIND_KEY_NEW_MIND}"
    assert "INFERENCE_PROXY_URL" not in env
    runtime = yaml.safe_load((tmp_path / "minds/new-mind/runtime.yaml").read_text())
    assert runtime["provider_connection"]["id"] == "direct"
    assert runtime["provider_connection"]["base_url"] == "https://direct.invalid/v1"


def test_new_mind_creation_stores_its_token_and_selected_connection(creation):
    client, minds, env, stack, state = creation
    saved = client.post("/api/settings/providers", json={"name": "Direct", "kind": "openai", "base_url": "http://direct.invalid/v1"})
    assert saved.status_code == 200
    response = client.post("/api/minds", json={"name": "direct-new", "harness": "dsh", "provider_id": saved.json()["id"], "provider_token": "mind-owned-token", "default_model": "direct-model"})
    assert response.status_code == 200, response.text
    assert credentials_data._read_env_value(env, "MIND_KEY_DIRECT_NEW") == "mind-owned-token"
    assert not state["proxy_requests"]
    runtime = yaml.safe_load((minds / "direct-new/runtime.yaml").read_text())
    assert runtime["provider_connection"]["base_url"] == "http://direct.invalid/v1"
    assert "mind-owned-token" not in response.text
    import json
    import subprocess
    result = subprocess.run(["docker", "compose", "--env-file", str(env), "-f", str(stack), "config", "--format", "json"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)["services"]["direct-new"]["environment"]
    assert actual["OPENAI_BASE_URL"] == "http://direct.invalid/v1"
    assert actual["DSH_API_KEY"] == "mind-owned-token"
    assert actual["HIVE_PROVIDER_TOKEN"] == "mind-owned-token"
    assert "INFERENCE_PROXY_URL" not in actual


async def test_url_edit_is_applied_on_reload_without_replacing_minds_keys(tmp_path, monkeypatch):
    own = tmp_path / "minds/smith"
    (own / "container").mkdir(parents=True)
    source = own / "runtime.yaml"
    source.write_text("name: smith\nharness: dsh\nprovider: proxy\ndefault_model: original-model\nenv:\n  OPENAI_API_KEY: runtime-owned-key\n")
    compose = own / "container/compose.yaml"
    compose.write_text("services:\n  smith:\n    environment:\n      - INFERENCE_PROXY_URL=http://old.invalid\n      - OPENAI_API_KEY=${PROXY_TOKEN_SMITH}\n      - UNRELATED=keep\n")
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_CONFIG_DIR", own.parent)
    monkeypatch.setenv("HIVE_MIND_PROXY_URL", "http://old.invalid")
    connection = await provider_data.connection_for_mind("smith")
    before = source.read_bytes(), compose.read_bytes()
    await provider_data.save_connection(dict(connection, base_url="http://new.invalid"), connection["id"])
    assert (source.read_bytes(), compose.read_bytes()) == before
    assert await provider_data.apply_pending_url("smith") is True
    runtime = yaml.safe_load(source.read_text())
    assert runtime["default_model"] == "original-model"
    assert runtime["env"]["OPENAI_API_KEY"] == "runtime-owned-key"
    actual = dict(item.split("=", 1) for item in yaml.safe_load(compose.read_text())["services"]["smith"]["environment"])
    assert actual["INFERENCE_PROXY_URL"] == "http://new.invalid"
    assert actual["OPENAI_API_KEY"] == "${PROXY_TOKEN_SMITH}"
    assert actual["UNRELATED"] == "keep"
    assert await provider_data.apply_pending_url("smith") is False


def test_provider_settings_and_model_discovery_require_admin(builtin_console):
    admin = builtin_console.admin()
    member = builtin_console.settled_user(admin, "member", "test-password-long")
    assert admin.get("/settings/providers").status_code == 200
    for client in (builtin_console.client(), member):
        assert client.get("/api/settings/providers").status_code in (401, 403)
        assert client.post("/api/settings/providers", json={"name": "Denied", "kind": "openai", "base_url": "http://not-used.invalid"}).status_code in (401, 403)
