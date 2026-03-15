"""Tests for voice_server.py configuration.

Verifies that:
- VOICE_REF_DIR env var has the correct default
- TTSRequest schema has the expected fields (voice_id, text, speed)
"""

import ast
import os

import pytest

VOICE_SERVER_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "voice", "voice_server.py"
)


@pytest.fixture
def voice_server_source() -> str:
    with open(VOICE_SERVER_PATH) as f:
        return f.read()


def test_voice_ref_dir_env_default(voice_server_source: str) -> None:
    """voice_server.py must read VOICE_REF_DIR env var with default /usr/src/app/voice_ref."""
    assert "VOICE_REF_DIR" in voice_server_source, "VOICE_REF_DIR env var not found"
    assert "/usr/src/app/voice_ref" in voice_server_source, (
        "Default VOICE_REF_DIR path not found"
    )


def test_tts_request_has_voice_id() -> None:
    """TTSRequest must have a voice_id field (str type)."""
    with open(VOICE_SERVER_PATH) as f:
        source = f.read()

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "TTSRequest":
            field_names = []
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    field_names.append(item.target.id)
            assert "voice_id" in field_names, "TTSRequest missing 'voice_id' field"
            return

    pytest.fail("TTSRequest class not found in voice_server.py")


def test_tts_request_has_text_and_speed() -> None:
    """TTSRequest must have text and speed fields."""
    with open(VOICE_SERVER_PATH) as f:
        source = f.read()

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "TTSRequest":
            field_names = []
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    field_names.append(item.target.id)
            assert "text" in field_names, "TTSRequest missing 'text' field"
            assert "speed" in field_names, "TTSRequest missing 'speed' field"
            return

    pytest.fail("TTSRequest class not found in voice_server.py")
