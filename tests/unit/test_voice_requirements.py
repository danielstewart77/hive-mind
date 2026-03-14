"""Tests for requirements.txt voice/TTS dependencies.

Verifies that:
- Coqui TTS package is present
- Core voice deps (whisper, soundfile, numpy, torch) remain
"""

import os

import pytest

REQUIREMENTS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "requirements.txt"
)


@pytest.fixture
def requirements_content() -> str:
    with open(REQUIREMENTS_PATH) as f:
        return f.read()


def test_tts_coqui_in_requirements(requirements_content: str) -> None:
    """requirements.txt must contain the Coqui TTS package."""
    lines = [line.strip().lower() for line in requirements_content.splitlines()]
    assert any(
        line.startswith("tts") and not line.startswith("tts_") for line in lines
    ), "requirements.txt must contain 'TTS' (Coqui) package"


def test_core_voice_deps_present(requirements_content: str) -> None:
    """Core voice deps must remain in requirements.txt."""
    required = ["faster-whisper", "soundfile", "numpy", "torch"]
    lower = requirements_content.lower()
    for dep in required:
        assert dep in lower, f"Core dep '{dep}' missing from requirements.txt"
