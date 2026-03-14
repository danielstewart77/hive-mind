"""Tests for docker-compose.yml voice config.

Verifies that:
- XTTS_REF_AUDIO and XTTS_LANGUAGE env vars are present in docker-compose.yml
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


def test_docker_compose_has_xtts_ref_audio(docker_compose_content: str) -> None:
    """docker-compose.yml must define XTTS_REF_AUDIO for voice-server."""
    assert "XTTS_REF_AUDIO" in docker_compose_content


def test_docker_compose_has_xtts_language(docker_compose_content: str) -> None:
    """docker-compose.yml must define XTTS_LANGUAGE for voice-server."""
    assert "XTTS_LANGUAGE" in docker_compose_content


