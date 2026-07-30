"""Creating an internal Mind from the console.

Before this existed the console could only check whether a name was free —
every one of the four harness/provider pairs running in this Hive today
(ada, bob, nagatha, bilby) had to be scaffolded by hand.
"""

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth
import configuration_data
import main as main_mod

_PAIRS = [
    ("claude_cli", "anthropic", "sonnet"),
    ("claude_cli", "ollama", "qwen3:8b"),
    ("codex_cli", "openai", "gpt-5.4"),
    ("codex_cli", "ollama", "qwen3:8b"),
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    minds_dir = tmp_path / "minds"
    minds_dir.mkdir()
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_DIR", minds_dir)
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_CONFIG_DIR", minds_dir)
    monkeypatch.setattr(main_mod, "_broker_minds_safe", AsyncMock(return_value=[]))
    user = auth.create_user("daniel", "secret-pass", is_admin=True, replace=True)
    test_client = TestClient(main_mod.app)
    test_client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
    test_client.minds_dir = minds_dir
    return test_client


def _create(client, **overrides):
    body = {"name": "atlas", "harness": "claude_cli", "provider": "anthropic"}
    body.update(overrides)
    return client.post("/api/minds", json=body)


@pytest.mark.parametrize("harness,provider,model", _PAIRS)
def test_every_pair_scaffolds_a_runnable_mind(client, harness, provider, model):
    response = _create(client, harness=harness, provider=provider)

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["registration"]["model"] == model

    runtime = yaml.safe_load((client.minds_dir / "atlas" / "runtime.yaml").read_text())
    assert runtime["harness"] == harness
    assert runtime["provider"] == provider
    assert runtime["default_model"] == model
    assert runtime["mind_id"] == body["mind_id"]
    assert runtime["remote"] is False

    compose = yaml.safe_load(
        (client.minds_dir / "atlas" / "container" / "compose.yaml").read_text()
    )
    service = compose["services"]["atlas"]
    assert service["container_name"] == "hive-mind-atlas"
    assert service["command"][-1] == f"minds.harness.{harness}"
    assert "hivemind" in service["networks"]


@pytest.mark.parametrize("harness,expected_var", [
    ("claude_cli", "CLAUDE_CONFIG_DIR"),
    ("codex_cli", "CODEX_HOME"),
])
def test_each_harness_gets_the_config_dir_variable_its_cli_reads(
    client, harness, expected_var
):
    provider = "anthropic" if harness == "claude_cli" else "openai"
    assert _create(client, harness=harness, provider=provider).status_code == 200

    compose = yaml.safe_load(
        (client.minds_dir / "atlas" / "container" / "compose.yaml").read_text()
    )
    environment = compose["services"]["atlas"]["environment"]
    assert any(item.startswith(f"{expected_var}=") for item in environment)

    runtime = yaml.safe_load((client.minds_dir / "atlas" / "runtime.yaml").read_text())
    suffix = ".claude" if harness == "claude_cli" else ".codex"
    assert runtime["runtime_config_dir"].endswith(suffix)


def test_ollama_env_block_is_the_whole_provider_switch(client):
    """The harness modules branch on this env block, not on `provider`."""
    assert _create(
        client, harness="claude_cli", provider="ollama",
        ollama_url="http://host.docker.internal:8899", ollama_api_key="hmp-abc",
    ).status_code == 200
    claude_env = yaml.safe_load(
        (client.minds_dir / "atlas" / "runtime.yaml").read_text()
    )["env"]
    assert claude_env["ANTHROPIC_BASE_URL"] == "http://host.docker.internal:8899"
    assert claude_env["ANTHROPIC_API_KEY"] == ""

    assert _create(
        client, name="bilby-two", harness="codex_cli", provider="ollama",
        ollama_url="http://host.docker.internal:8899", ollama_api_key="hmp-abc",
    ).status_code == 200
    codex_env = yaml.safe_load(
        (client.minds_dir / "bilby-two" / "runtime.yaml").read_text()
    )["env"]
    # Codex speaks the OpenAI-compatible dialect, served under /v1.
    assert codex_env["OLLAMA_BASE_URL"] == "http://host.docker.internal:8899/v1"
    assert codex_env["OPENAI_API_KEY"] == "hmp-abc"


def test_a_first_party_mind_gets_no_env_block(client):
    assert _create(client).status_code == 200
    runtime = yaml.safe_load((client.minds_dir / "atlas" / "runtime.yaml").read_text())
    assert "env" not in runtime


def test_ollama_defaults_to_the_metering_proxy(client, monkeypatch):
    """Every Ollama mind in this hive goes through the inference proxy so its
    usage is attributable; defaulting to raw Ollama would silently skip that."""
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    with patch.object(main_mod, "get_capability_endpoint", return_value=""):
        assert _create(client, provider="ollama").status_code == 200

    runtime = yaml.safe_load((client.minds_dir / "atlas" / "runtime.yaml").read_text())
    assert runtime["env"]["ANTHROPIC_BASE_URL"] == (
        configuration_data.DEFAULT_OLLAMA_BASE_URL
    )


def test_rejects_pairs_the_cli_cannot_serve(client):
    for harness, provider in [("claude_cli", "openai"), ("codex_cli", "anthropic")]:
        response = _create(client, harness=harness, provider=provider)
        assert response.status_code == 400
        assert provider in response.json()["detail"]
    assert not (client.minds_dir / "atlas").exists()


def test_rejects_a_bad_name_before_writing_anything(client):
    for name in ["../bad", "A", "Atlas!", ""]:
        assert _create(client, name=name).status_code == 400
    assert list(client.minds_dir.iterdir()) == []


def test_refuses_to_overwrite_a_name_the_broker_knows(client):
    with patch.object(
        main_mod, "_broker_minds_safe", new=AsyncMock(return_value=[{"name": "atlas"}])
    ):
        response = _create(client)
    assert response.status_code == 409
    assert not (client.minds_dir / "atlas").exists()


def test_refuses_to_overwrite_an_existing_scaffold(client):
    """A mind scaffolded but never registered is invisible to the broker —
    checking only the broker would clobber it."""
    assert _create(client).status_code == 200
    assert _create(client).status_code == 409


def test_check_name_sees_unregistered_scaffolds(client):
    assert client.get("/api/settings/new-mind/check-name?name=atlas").json()["available"]
    assert _create(client).status_code == 200
    assert not client.get(
        "/api/settings/new-mind/check-name?name=atlas"
    ).json()["available"]


def test_register_sends_the_scaffolded_identity_to_the_broker(client):
    assert _create(client, harness="codex_cli", provider="openai").status_code == 200
    mind_id = yaml.safe_load(
        (client.minds_dir / "atlas" / "runtime.yaml").read_text()
    )["mind_id"]

    gateway = AsyncMock(return_value={"status": "registered"})
    with patch.object(main_mod, "_gateway_request", new=gateway):
        response = client.post("/api/minds/atlas/register", json={})

    assert response.status_code == 200
    assert response.json()["registered"] is True
    payload = gateway.await_args.kwargs["json_body"]
    assert payload == {
        "mind_id": mind_id,
        "name": "atlas",
        "gateway_url": "http://atlas:8420",
        "model": "gpt-5.4",
        # Already the broker's vocabulary — no translation step.
        "harness": "codex_cli",
    }


def test_register_rejects_a_mind_that_was_never_scaffolded(client):
    response = client.post("/api/minds/ghost/register", json={})
    assert response.status_code == 400


def test_the_form_offers_only_pairs_the_api_accepts(client):
    page = client.get("/minds/new").text
    assert 'value="claude_cli"' in page
    assert 'value="codex_cli"' in page
    # Identity is deliberately not scaffolded here.
    assert "soul" in page.lower()
