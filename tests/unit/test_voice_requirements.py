"""Tests for voice server requirements dependencies.

Verifies that:
- Chatterbox dependencies are present in requirements.voice.txt
- Core voice deps (whisper, soundfile, numpy, torch, fastapi, uvicorn) are present
- Main requirements.txt does not contain voice/TTS-specific deps
"""

import os

import pytest

REQUIREMENTS_VOICE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "requirements.voice.txt"
)
REQUIREMENTS_MAIN_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "requirements.txt"
)


@pytest.fixture
def voice_requirements_content() -> str:
    with open(REQUIREMENTS_VOICE_PATH) as f:
        return f.read()


@pytest.fixture
def main_requirements_content() -> str:
    with open(REQUIREMENTS_MAIN_PATH) as f:
        return f.read()


def test_chatterbox_deps_in_requirements(voice_requirements_content: str) -> None:
    """requirements.voice.txt must contain Chatterbox-specific dependencies."""
    lower = voice_requirements_content.lower()
    required_deps = [
        "torch==2.6.0",
        "torchaudio==2.6.0",
        "transformers==4.46.3",
        "resemble-perth",
        "conformer",
        "s3tokenizer",
        "diffusers",
    ]
    for dep in required_deps:
        assert dep.lower() in lower, f"Chatterbox dep '{dep}' missing from requirements.voice.txt"


def test_core_voice_deps_present(voice_requirements_content: str) -> None:
    """Core voice deps must be present in requirements.voice.txt."""
    lower = voice_requirements_content.lower()
    required = ["faster-whisper", "soundfile", "numpy", "torch", "fastapi", "uvicorn"]
    for dep in required:
        assert dep in lower, f"Core dep '{dep}' missing from requirements.voice.txt"


def test_main_requirements_no_voice_deps(main_requirements_content: str) -> None:
    """Main requirements.txt must not contain voice/TTS-specific deps.

    Voice deps belong only in requirements.voice.txt (used by Dockerfile.voice).
    The main server image does not need torch or TTS libraries.
    """
    lower = main_requirements_content.lower()
    voice_only_deps = ["torch==", "torchaudio==", "tts>=", "faster-whisper", "soundfile"]
    for dep in voice_only_deps:
        assert dep.lower() not in lower, (
            f"Voice-only dep '{dep}' should not be in main requirements.txt"
        )
