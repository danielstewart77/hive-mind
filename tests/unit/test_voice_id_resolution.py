"""Tests for voice ID resolution logic.

Verifies that:
- _resolve_voice_ref returns the correct WAV path when the file exists
- _resolve_voice_ref falls back to default.wav when the requested voice doesn't exist
- _resolve_voice_ref("default") maps to default.wav
"""

import sys
from unittest.mock import MagicMock, patch

import pytest


def _can_import(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


_NEED_PYDANTIC_MOCK = not _can_import("pydantic")


@pytest.fixture(autouse=True)
def _mock_voice_server_deps(monkeypatch):
    """Mock heavy deps so voice_server can be imported without GPU libs."""
    np_mock = MagicMock()
    np_mock.float32 = "float32"
    np_mock.ndarray = type("ndarray", (), {})
    np_mock.array = MagicMock(side_effect=lambda x, dtype=None: x)
    monkeypatch.setitem(sys.modules, "numpy", np_mock)

    torch_mock = MagicMock()
    torch_mock.cuda.is_available.return_value = False
    monkeypatch.setitem(sys.modules, "torch", torch_mock)

    monkeypatch.setitem(sys.modules, "torchaudio", MagicMock())
    monkeypatch.setitem(sys.modules, "faster_whisper", MagicMock())
    monkeypatch.setitem(sys.modules, "soundfile", MagicMock())

    chatterbox_mod = MagicMock()
    monkeypatch.setitem(sys.modules, "chatterbox", chatterbox_mod)
    monkeypatch.setitem(sys.modules, "chatterbox.tts", chatterbox_mod.tts)

    # Mock pydantic/fastapi if pydantic_core native lib can't load (CI/read-only fs)
    if _NEED_PYDANTIC_MOCK:
        pydantic_mock = MagicMock()
        pydantic_mock.BaseModel = type("BaseModel", (), {})
        monkeypatch.setitem(sys.modules, "pydantic", pydantic_mock)
        monkeypatch.setitem(sys.modules, "pydantic_core", MagicMock())

        fastapi_mock = MagicMock()
        monkeypatch.setitem(sys.modules, "fastapi", fastapi_mock)
        monkeypatch.setitem(sys.modules, "fastapi.responses", MagicMock())

    for mod_name in list(sys.modules.keys()):
        if "voice_server" in mod_name or "voice.voice_server" in mod_name:
            del sys.modules[mod_name]

    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")


def _import_voice_server():
    """Import voice_server with GPU check bypassed."""
    with patch("ctypes.CDLL", side_effect=OSError("no GPU")):
        for mod_name in list(sys.modules.keys()):
            if "voice_server" in mod_name or "voice.voice_server" in mod_name:
                del sys.modules[mod_name]
        import voice.voice_server as vs
        return vs


def test_resolve_voice_id_existing_file(tmp_path) -> None:
    """_resolve_voice_ref returns the path when the requested voice file exists."""
    vs = _import_voice_server()

    # Create a fake voice file
    ada_wav = tmp_path / "ada.wav"
    ada_wav.write_bytes(b"RIFF" + b"\x00" * 100)

    result = vs._resolve_voice_ref("ada", str(tmp_path))
    assert result == str(ada_wav)


def test_resolve_voice_id_fallback_to_default(tmp_path) -> None:
    """_resolve_voice_ref falls back to default.wav when the requested voice doesn't exist."""
    vs = _import_voice_server()

    # Only create default.wav, not the requested voice
    default_wav = tmp_path / "default.wav"
    default_wav.write_bytes(b"RIFF" + b"\x00" * 100)

    result = vs._resolve_voice_ref("nonexistent", str(tmp_path))
    assert result == str(default_wav)


def test_resolve_voice_id_default_maps_to_default_wav(tmp_path) -> None:
    """_resolve_voice_ref("default") returns default.wav."""
    vs = _import_voice_server()

    default_wav = tmp_path / "default.wav"
    default_wav.write_bytes(b"RIFF" + b"\x00" * 100)

    result = vs._resolve_voice_ref("default", str(tmp_path))
    assert result == str(default_wav)


def test_resolve_voice_id_returns_none_when_nothing_exists(tmp_path) -> None:
    """_resolve_voice_ref returns None when neither the voice nor default exists."""
    vs = _import_voice_server()

    result = vs._resolve_voice_ref("nonexistent", str(tmp_path))
    assert result is None
