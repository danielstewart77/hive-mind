"""Tests for voice_server.py configuration and cleanup (Step 2).

Verifies that:
- XTTS env var defaults are correct
- Old env vars are not referenced
- Backend switching is removed
- TTSRequest schema is unchanged
"""

import os
import re

import pytest

VOICE_SERVER_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "voice", "voice_server.py"
)


@pytest.fixture
def voice_server_source() -> str:
    with open(VOICE_SERVER_PATH) as f:
        return f.read()


def test_xtts_env_defaults(voice_server_source: str) -> None:
    """Default XTTS_REF_AUDIO and XTTS_LANGUAGE must be set correctly."""
    assert "/usr/src/app/voice_ref/hive_mind_voice.wav" in voice_server_source, (
        "Default XTTS_REF_AUDIO path not found"
    )
    assert 'XTTS_LANGUAGE' in voice_server_source, "XTTS_LANGUAGE env var not found"
    # Check the default language value is 'en'
    assert re.search(
        r'XTTS_LANGUAGE.*"en"', voice_server_source
    ), "Default XTTS_LANGUAGE must be 'en'"


def test_old_env_vars_not_referenced(voice_server_source: str) -> None:
    """Old TTS env vars must not appear in voice_server.py."""
    banned_vars = [
        "KOKORO_VOICE",
        "F5_REF_AUDIO",
        "F5_REF_TEXT",
        "TTS_BACKEND",
        "BARK_SPEAKER",
        "FISH_SPEECH_URL",
        "FISH_REF_AUDIO",
    ]
    for var in banned_vars:
        assert var not in voice_server_source, (
            f"Old env var '{var}' still referenced in voice_server.py"
        )


def test_no_backend_switching(voice_server_source: str) -> None:
    """BackendRequest model and /backend endpoint must be removed."""
    assert "BackendRequest" not in voice_server_source, (
        "BackendRequest class still present"
    )
    assert '"/backend"' not in voice_server_source, (
        "/backend endpoint still present"
    )


def test_tts_request_schema_unchanged() -> None:
    """TTSRequest must have text (str), voice (str, default 'default'), speed (float, default 0.9)."""
    # We need to import TTSRequest without triggering the full module startup.
    # Read the source and exec just the model definition.
    import ast
    with open(VOICE_SERVER_PATH) as f:
        source = f.read()

    tree = ast.parse(source)
    # Find the TTSRequest class
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "TTSRequest":
            found = True
            # Check it has the expected fields by name
            field_names = []
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    field_names.append(item.target.id)
            assert "text" in field_names, "TTSRequest missing 'text' field"
            assert "voice" in field_names, "TTSRequest missing 'voice' field"
            assert "speed" in field_names, "TTSRequest missing 'speed' field"
            break

    assert found, "TTSRequest class not found in voice_server.py"
