"""Tests that the compose template wires the Kokoro voice service correctly.

Verifies the voice-server-kokoro service exists, selects the Kokoro engine,
exposes host port 8423, and registers its own cache volume — without touching
the existing Chatterbox voice-server service. Reads docker-compose.example.yml
because the live docker-compose.yml is host-specific and gitignored.
"""

import os

import pytest

COMPOSE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "docker-compose.example.yml"
)


@pytest.fixture
def compose_content() -> str:
    with open(COMPOSE_PATH) as f:
        return f.read()


def test_kokoro_service_defined(compose_content: str) -> None:
    assert "voice-server-kokoro:" in compose_content


def test_kokoro_service_uses_kokoro_dockerfile(compose_content: str) -> None:
    assert "Dockerfile.voice.kokoro" in compose_content


def test_kokoro_service_selects_engine(compose_content: str) -> None:
    assert "TTS_ENGINE=kokoro" in compose_content


def test_kokoro_service_exposes_host_port_8423(compose_content: str) -> None:
    assert '"8423:8422"' in compose_content


def test_kokoro_cache_volume_registered(compose_content: str) -> None:
    assert "kokoro-cache:" in compose_content


def test_chatterbox_service_still_present(compose_content: str) -> None:
    """The original cloning voice-server must be untouched."""
    assert "container_name: hive-mind-voice\n" in compose_content
    assert '"8422:8422"' in compose_content
