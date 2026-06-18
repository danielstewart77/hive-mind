"""Tests for the Kokoro voice server requirements.

Verifies that:
- requirements.voice.kokoro.txt contains Kokoro + core voice deps
- it does NOT contain Chatterbox-only deps (the whole point of a separate image)
"""

import os

import pytest

REQUIREMENTS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "requirements.voice.kokoro.txt"
)


@pytest.fixture
def requirements_content() -> str:
    with open(REQUIREMENTS_PATH) as f:
        return f.read()


def test_kokoro_dep_present(requirements_content: str) -> None:
    assert "kokoro" in requirements_content.lower()


def test_core_voice_deps_present(requirements_content: str) -> None:
    lower = requirements_content.lower()
    required = ["faster-whisper", "soundfile", "torch", "torchaudio", "fastapi", "uvicorn"]
    for dep in required:
        assert dep in lower, f"Core dep '{dep}' missing from requirements.voice.kokoro.txt"


def test_no_chatterbox_only_deps(requirements_content: str) -> None:
    """Chatterbox-specific deps must not leak into the Kokoro image."""
    lower = requirements_content.lower()
    chatterbox_only = ["resemble-perth", "conformer", "s3tokenizer", "diffusers"]
    for dep in chatterbox_only:
        assert dep not in lower, (
            f"Chatterbox-only dep '{dep}' should not be in requirements.voice.kokoro.txt"
        )
