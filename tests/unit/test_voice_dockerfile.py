"""Tests for Dockerfile.voice build configuration.

Verifies that the Dockerfile.voice correctly configures the Chatterbox
TTS build environment including setuptools pin, --no-deps install, and
build-time import validation.
"""

import os

import pytest

DOCKERFILE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "Dockerfile.voice"
)


@pytest.fixture
def dockerfile_content() -> str:
    with open(DOCKERFILE_PATH) as f:
        return f.read()


def test_dockerfile_has_setuptools_pin(dockerfile_content: str) -> None:
    """Dockerfile.voice must pin setuptools<81 for resemble-perth compatibility."""
    assert "setuptools<81" in dockerfile_content, (
        "Dockerfile.voice must contain 'setuptools<81' in a pip install line"
    )


def test_dockerfile_has_chatterbox_no_deps(dockerfile_content: str) -> None:
    """Dockerfile.voice must install chatterbox-tts with --no-deps."""
    assert "--no-deps chatterbox-tts" in dockerfile_content, (
        "Dockerfile.voice must install chatterbox-tts with --no-deps"
    )


def test_dockerfile_validation_imports_chatterbox(dockerfile_content: str) -> None:
    """Build-time validation must import ChatterboxTTS to catch broken deps early."""
    assert "from chatterbox.tts import ChatterboxTTS" in dockerfile_content, (
        "Dockerfile.voice build validation must import ChatterboxTTS"
    )


def test_dockerfile_validation_imports_whisper(dockerfile_content: str) -> None:
    """Build-time validation must import WhisperModel to verify STT deps."""
    assert "from faster_whisper import WhisperModel" in dockerfile_content, (
        "Dockerfile.voice build validation must import WhisperModel"
    )
