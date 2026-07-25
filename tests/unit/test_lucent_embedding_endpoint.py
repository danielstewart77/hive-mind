"""Tests that lucent's embedding endpoint is reachable from inside its container.

`lucent_memory._embed` defaults `OLLAMA_BASE_URL` to loopback so a clone of
this repo carries no LAN address. Inside hive-lucent, loopback is the
container itself and nothing listens there, so every /memory/retrieve and
/memory/store fails with connection refused unless compose overrides the
default. The same applies to the nightly pruner's hive-tools classifier.

Reads docker-compose.example.yml because the live docker-compose.yml is
host-specific and gitignored.
"""

import os

import pytest
import yaml

COMPOSE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "docker-compose.example.yml"
)


@pytest.fixture
def lucent_service() -> dict:
    with open(COMPOSE_PATH) as f:
        return yaml.safe_load(f)["services"]["lucent"]


def test_lucent_sets_ollama_base_url(lucent_service: dict) -> None:
    assert "OLLAMA_BASE_URL" in lucent_service["environment"]


def test_lucent_ollama_base_url_is_not_container_loopback(
    lucent_service: dict,
) -> None:
    value = lucent_service["environment"]["OLLAMA_BASE_URL"]
    assert "127.0.0.1" not in value
    assert "localhost" not in value


def test_lucent_resolves_host_gateway(lucent_service: dict) -> None:
    """host.docker.internal only resolves on Linux with an explicit mapping."""
    assert "host.docker.internal:host-gateway" in lucent_service["extra_hosts"]


def test_lucent_ollama_default_targets_the_mapped_host(
    lucent_service: dict,
) -> None:
    default = lucent_service["environment"]["OLLAMA_BASE_URL"]
    assert "${OLLAMA_BASE_URL:-http://host.docker.internal:11434}" == default


def test_lucent_pruner_reaches_hive_tools_by_service_name(
    lucent_service: dict,
) -> None:
    """prune_memory's default is loopback too; hive-tools is a sibling."""
    assert lucent_service["environment"]["HIVE_TOOLS_URL"] == (
        "http://hive-tools:9421"
    )
