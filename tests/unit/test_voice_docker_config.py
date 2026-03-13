"""Tests for docker-compose.yml and Dockerfile voice config (Step 3).

Verifies that:
- XTTS_REF_AUDIO and XTTS_LANGUAGE env vars are present in docker-compose.yml
- Old env vars are removed from docker-compose.yml
- Chatterbox pip install is removed from Dockerfile
"""

import os

import pytest

DOCKER_COMPOSE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "docker-compose.yml"
)
DOCKERFILE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "Dockerfile"
)


@pytest.fixture
def docker_compose_content() -> str:
    with open(DOCKER_COMPOSE_PATH) as f:
        return f.read()


@pytest.fixture
def dockerfile_content() -> str:
    with open(DOCKERFILE_PATH) as f:
        return f.read()


def test_docker_compose_has_xtts_ref_audio(docker_compose_content: str) -> None:
    """docker-compose.yml must define XTTS_REF_AUDIO for voice-server."""
    assert "XTTS_REF_AUDIO" in docker_compose_content


def test_docker_compose_has_xtts_language(docker_compose_content: str) -> None:
    """docker-compose.yml must define XTTS_LANGUAGE for voice-server."""
    assert "XTTS_LANGUAGE" in docker_compose_content


def test_docker_compose_no_old_env_vars(docker_compose_content: str) -> None:
    """Old TTS env vars must not appear in docker-compose.yml."""
    banned = [
        "KOKORO_VOICE",
        "F5_REF_AUDIO",
        "F5_REF_TEXT",
        "TTS_BACKEND",
        "BARK_SPEAKER",
        "FISH_REF_AUDIO",
    ]
    for var in banned:
        assert var not in docker_compose_content, (
            f"Old env var '{var}' still in docker-compose.yml"
        )


def test_docker_compose_no_chatterbox_install(dockerfile_content: str) -> None:
    """Dockerfile must not have chatterbox-tts pip install."""
    assert "chatterbox-tts" not in dockerfile_content
