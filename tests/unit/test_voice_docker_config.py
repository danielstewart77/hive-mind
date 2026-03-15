"""Tests for docker-compose.yml voice server configuration.

Verifies that:
- VOICE_REF_DIR env var is present
- WHISPER_MODEL env var is present
- voice-server service uses Dockerfile.voice
"""

import os

import pytest

DOCKER_COMPOSE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "docker-compose.yml"
)


@pytest.fixture
def docker_compose_content() -> str:
    with open(DOCKER_COMPOSE_PATH) as f:
        return f.read()


def test_docker_compose_has_voice_ref_dir(docker_compose_content: str) -> None:
    """docker-compose.yml must define VOICE_REF_DIR for voice-server."""
    assert "VOICE_REF_DIR" in docker_compose_content


def test_docker_compose_has_whisper_model(docker_compose_content: str) -> None:
    """docker-compose.yml must define WHISPER_MODEL for voice-server."""
    assert "WHISPER_MODEL" in docker_compose_content


def test_voice_server_uses_dockerfile_voice(docker_compose_content: str) -> None:
    """voice-server service must use Dockerfile.voice, not the default Dockerfile."""
    assert "Dockerfile.voice" in docker_compose_content
