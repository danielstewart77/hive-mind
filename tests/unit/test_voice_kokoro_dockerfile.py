"""Tests for Dockerfile.voice.kokoro build configuration.

Verifies that the Kokoro voice image installs Kokoro + espeak-ng, validates its
deps at build time, selects the Kokoro engine, and does NOT drag in Chatterbox.
"""

import os

import pytest

DOCKERFILE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "Dockerfile.voice.kokoro"
)


@pytest.fixture
def dockerfile_content() -> str:
    with open(DOCKERFILE_PATH) as f:
        return f.read()


def test_dockerfile_installs_kokoro_requirements(dockerfile_content: str) -> None:
    assert "requirements.voice.kokoro.txt" in dockerfile_content


def test_dockerfile_installs_espeak_ng(dockerfile_content: str) -> None:
    """Kokoro's G2P fallback needs espeak-ng as a system package."""
    assert "espeak-ng" in dockerfile_content


def test_dockerfile_validation_imports_kpipeline(dockerfile_content: str) -> None:
    assert "from kokoro import KPipeline" in dockerfile_content


def test_dockerfile_validation_imports_whisper(dockerfile_content: str) -> None:
    assert "from faster_whisper import WhisperModel" in dockerfile_content


def test_dockerfile_bakes_spacy_model(dockerfile_content: str) -> None:
    """misaki's English G2P needs en_core_web_sm baked in; the read-only
    container can't pip-download it at runtime."""
    assert "spacy download en_core_web_sm" in dockerfile_content


def test_dockerfile_sets_kokoro_engine(dockerfile_content: str) -> None:
    assert "TTS_ENGINE=kokoro" in dockerfile_content


def test_dockerfile_does_not_install_chatterbox(dockerfile_content: str) -> None:
    """The Kokoro image must stay free of the Chatterbox stack."""
    assert "chatterbox" not in dockerfile_content.lower()
