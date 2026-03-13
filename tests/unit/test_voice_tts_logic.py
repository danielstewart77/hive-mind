"""Tests for XTTS v2 TTS synthesis logic (Step 2).

Verifies that:
- When ref audio exists, tts_with_vc() is called with correct args
- When ref audio is missing, tts() fallback with Claribel Dervla is used
- _synthesize returns a numpy array
"""

import sys
import types
from unittest.mock import MagicMock, patch

import pytest


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

    # Mock TTS.api
    tts_mod = types.ModuleType("TTS")
    tts_api_mod = types.ModuleType("TTS.api")
    tts_api_mod.TTS = MagicMock()
    monkeypatch.setitem(sys.modules, "TTS", tts_mod)
    monkeypatch.setitem(sys.modules, "TTS.api", tts_api_mod)

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


def test_tts_with_ref_audio_calls_tts_with_vc() -> None:
    """When ref audio file exists, _synthesize must call tts_with_vc()."""
    vs = _import_voice_server()

    mock_model = MagicMock()
    mock_model.tts_with_vc.return_value = [0.0] * 100
    vs._xtts_model = mock_model
    vs._use_voice_cloning = True

    vs._synthesize("Hello Daniel")

    mock_model.tts_with_vc.assert_called_once_with(
        text="Hello Daniel",
        speaker_wav=vs._XTTS_REF_AUDIO,
        language=vs._XTTS_LANGUAGE,
    )
    mock_model.tts.assert_not_called()


def test_tts_without_ref_audio_calls_fallback() -> None:
    """When ref audio is missing, must fall back to stock speaker Claribel Dervla."""
    vs = _import_voice_server()

    mock_model = MagicMock()
    mock_model.tts.return_value = [0.0] * 100
    vs._xtts_model = mock_model
    vs._use_voice_cloning = False

    vs._synthesize("Hello world")

    mock_model.tts.assert_called_once_with(
        text="Hello world",
        speaker="Claribel Dervla",
        language=vs._XTTS_LANGUAGE,
    )
    mock_model.tts_with_vc.assert_not_called()


def test_synthesize_raises_when_model_not_loaded() -> None:
    """_synthesize must raise RuntimeError when TTS model is not loaded."""
    vs = _import_voice_server()
    vs._xtts_model = None

    with pytest.raises(RuntimeError, match="TTS model not loaded"):
        vs._synthesize("test text")


def test_synthesize_returns_audio_data() -> None:
    """_synthesize must return audio data from the TTS model."""
    vs = _import_voice_server()

    expected_audio = [0.1, 0.2, 0.3, 0.4]
    mock_model = MagicMock()
    mock_model.tts.return_value = expected_audio
    vs._xtts_model = mock_model
    vs._use_voice_cloning = False

    result = vs._synthesize("test")

    # np.array mock returns the input directly (via side_effect=lambda x, dtype=None: x)
    assert result == expected_audio


def test_synthesize_uses_language_config() -> None:
    """_synthesize must pass the configured language to the TTS model."""
    vs = _import_voice_server()

    mock_model = MagicMock()
    mock_model.tts_with_vc.return_value = [0.0] * 100
    vs._xtts_model = mock_model
    vs._use_voice_cloning = True

    # Change language to verify it's used
    original_lang = vs._XTTS_LANGUAGE
    vs._XTTS_LANGUAGE = "es"
    try:
        vs._synthesize("Hola mundo")
        mock_model.tts_with_vc.assert_called_once_with(
            text="Hola mundo",
            speaker_wav=vs._XTTS_REF_AUDIO,
            language="es",
        )
    finally:
        vs._XTTS_LANGUAGE = original_lang
