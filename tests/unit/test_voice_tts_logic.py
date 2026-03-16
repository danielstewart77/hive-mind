"""Tests for Chatterbox TTS synthesis logic.

Verifies that:
- _synthesize calls model.generate with correct args when model is loaded
- _synthesize raises RuntimeError when model is not loaded
- _synthesize works with and without a reference audio path
- tts() endpoint handler calls _synthesize_chunked (not _synthesize directly)
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
    # Mock numpy
    np_mock = MagicMock()
    np_mock.float32 = "float32"
    np_mock.ndarray = type("ndarray", (), {})
    np_mock.array = MagicMock(side_effect=lambda x, dtype=None: x)
    monkeypatch.setitem(sys.modules, "numpy", np_mock)

    # Mock torch
    torch_mock = MagicMock()
    torch_mock.cuda.is_available.return_value = False
    monkeypatch.setitem(sys.modules, "torch", torch_mock)

    # Mock torchaudio
    monkeypatch.setitem(sys.modules, "torchaudio", MagicMock())

    # Mock faster_whisper
    monkeypatch.setitem(sys.modules, "faster_whisper", MagicMock())

    # Mock soundfile
    monkeypatch.setitem(sys.modules, "soundfile", MagicMock())

    # Mock chatterbox.tts
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

    # Remove cached voice_server module to force reimport with mocks
    for mod_name in list(sys.modules.keys()):
        if "voice_server" in mod_name or "voice.voice_server" in mod_name:
            del sys.modules[mod_name]

    # Patch _check_gpu_early before import
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")


def _import_voice_server():
    """Import voice_server with GPU check bypassed."""
    with patch("ctypes.CDLL", side_effect=OSError("no GPU")):
        for mod_name in list(sys.modules.keys()):
            if "voice_server" in mod_name or "voice.voice_server" in mod_name:
                del sys.modules[mod_name]
        import voice.voice_server as vs
        return vs


def test_synthesize_calls_chatterbox_generate() -> None:
    """When model is loaded and ref audio path is given, _synthesize calls model.generate."""
    vs = _import_voice_server()

    mock_model = MagicMock()
    mock_model.generate.return_value = MagicMock()  # tensor
    vs._chatterbox_model = mock_model

    ref_path = "/usr/src/app/voice_ref/ada.wav"
    vs._synthesize("Hello Daniel", ref_path)

    mock_model.generate.assert_called_once_with(
        "Hello Daniel", audio_prompt_path=ref_path
    )


def test_synthesize_raises_when_model_not_loaded() -> None:
    """_synthesize must raise RuntimeError when TTS model is not loaded."""
    vs = _import_voice_server()
    vs._chatterbox_model = None

    with pytest.raises(RuntimeError, match="TTS model not loaded"):
        vs._synthesize("test text")


def test_synthesize_without_ref_path() -> None:
    """When ref_path is None, model.generate is called with audio_prompt_path=None."""
    vs = _import_voice_server()

    mock_model = MagicMock()
    mock_model.generate.return_value = MagicMock()
    vs._chatterbox_model = mock_model

    vs._synthesize("Hello world", None)

    mock_model.generate.assert_called_once_with(
        "Hello world", audio_prompt_path=None
    )


def test_tts_endpoint_handler_calls_synthesize_chunked() -> None:
    """The tts() endpoint handler must call _synthesize_chunked, not _synthesize directly.

    Since pydantic may not be available (CI/read-only fs), we verify by
    reading the source file and checking the tts() function body.
    """
    import pathlib

    source_path = pathlib.Path(__file__).resolve().parents[2] / "voice" / "voice_server.py"
    source = source_path.read_text()

    # Extract the tts endpoint function body (from 'async def tts' to next function/section)
    lines = source.splitlines()
    in_tts = False
    tts_body: list[str] = []
    for line in lines:
        if "async def tts(" in line:
            in_tts = True
            continue
        if in_tts:
            # End of function: non-indented line that's not empty/comment
            if line and not line.startswith(" ") and not line.startswith("\t"):
                break
            tts_body.append(line)

    tts_source = "\n".join(tts_body)

    # It must call _synthesize_chunked
    assert "_synthesize_chunked(" in tts_source, (
        "tts() endpoint should call _synthesize_chunked"
    )
    # It must NOT call bare _synthesize (only _synthesize_chunked)
    stripped = tts_source.replace("_synthesize_chunked", "")
    assert "_synthesize(" not in stripped, (
        "tts() endpoint should not call _synthesize directly"
    )
