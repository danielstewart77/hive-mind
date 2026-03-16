"""Tests for _synthesize_chunked() in voice_server.

Verifies:
- Single sentence delegates to _synthesize directly (no concatenation)
- Multiple sentences calls _synthesize per chunk, concatenates with torch.cat
- ref_path is forwarded to each _synthesize call
- torch.cat is called with dim=-1 (time axis)
- Fallback to single-call _synthesize on chunk error
- Fallback to single-call _synthesize on concat error
- RuntimeError propagates when model is not loaded
"""

import sys
from unittest.mock import MagicMock, call, patch

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


def test_synthesize_chunked_single_sentence() -> None:
    """Single sentence calls _synthesize once and returns its result directly."""
    vs = _import_voice_server()

    mock_model = MagicMock()
    single_tensor = MagicMock()
    mock_model.generate.return_value = single_tensor
    vs._chatterbox_model = mock_model

    result = vs._synthesize_chunked("Hello world.", "/fake/ref.wav")

    mock_model.generate.assert_called_once_with(
        "Hello world.", audio_prompt_path="/fake/ref.wav"
    )
    assert result is single_tensor


def test_synthesize_chunked_multiple_sentences() -> None:
    """Multi-sentence text calls _synthesize per sentence, returns torch.cat result."""
    vs = _import_voice_server()
    torch_mod = sys.modules["torch"]

    mock_model = MagicMock()
    tensor_a = MagicMock()
    tensor_b = MagicMock()
    mock_model.generate.side_effect = [tensor_a, tensor_b]
    vs._chatterbox_model = mock_model

    concat_result = MagicMock()
    torch_mod.cat.return_value = concat_result

    result = vs._synthesize_chunked("First sentence. Second sentence.", "/fake/ref.wav")

    assert mock_model.generate.call_count == 2
    assert result is concat_result


def test_synthesize_chunked_passes_ref_path() -> None:
    """ref_path is forwarded to each _synthesize call."""
    vs = _import_voice_server()
    torch_mod = sys.modules["torch"]
    torch_mod.cat.return_value = MagicMock()

    mock_model = MagicMock()
    mock_model.generate.return_value = MagicMock()
    vs._chatterbox_model = mock_model

    ref = "/usr/src/app/voice_ref/ada.wav"
    vs._synthesize_chunked("One. Two. Three.", ref)

    for c in mock_model.generate.call_args_list:
        assert c == call(c[0][0], audio_prompt_path=ref)


def test_synthesize_chunked_concatenates_along_time_axis() -> None:
    """torch.cat must be called with dim=-1 (time axis)."""
    vs = _import_voice_server()
    torch_mod = sys.modules["torch"]

    mock_model = MagicMock()
    t1, t2 = MagicMock(), MagicMock()
    mock_model.generate.side_effect = [t1, t2]
    vs._chatterbox_model = mock_model

    concat_result = MagicMock()
    torch_mod.cat.return_value = concat_result

    vs._synthesize_chunked("Hello world. Goodbye world.", "/fake/ref.wav")

    torch_mod.cat.assert_called_once()
    _, kwargs = torch_mod.cat.call_args
    assert kwargs.get("dim") == -1


def test_synthesize_chunked_fallback_on_chunk_error() -> None:
    """If a chunk's _synthesize raises, falls back to single-call _synthesize."""
    vs = _import_voice_server()

    mock_model = MagicMock()
    fallback_tensor = MagicMock()
    # First call succeeds, second raises, then fallback succeeds
    mock_model.generate.side_effect = [
        MagicMock(),
        RuntimeError("chunk failed"),
        fallback_tensor,
    ]
    vs._chatterbox_model = mock_model

    result = vs._synthesize_chunked("First ok. Second fails.", "/fake/ref.wav")

    # The fallback should call _synthesize with the full text
    assert result is fallback_tensor
    # Last call should be the full text (fallback)
    last_call = mock_model.generate.call_args_list[-1]
    assert last_call == call("First ok. Second fails.", audio_prompt_path="/fake/ref.wav")


def test_synthesize_chunked_fallback_on_concat_error() -> None:
    """If torch.cat raises, falls back to single-call _synthesize."""
    vs = _import_voice_server()
    torch_mod = sys.modules["torch"]

    mock_model = MagicMock()
    fallback_tensor = MagicMock()
    mock_model.generate.side_effect = [MagicMock(), MagicMock(), fallback_tensor]
    vs._chatterbox_model = mock_model

    torch_mod.cat.side_effect = RuntimeError("concat failed")

    result = vs._synthesize_chunked("Sentence one. Sentence two.", "/fake/ref.wav")

    assert result is fallback_tensor
    last_call = mock_model.generate.call_args_list[-1]
    assert last_call == call("Sentence one. Sentence two.", audio_prompt_path="/fake/ref.wav")


def test_synthesize_chunked_raises_when_model_not_loaded() -> None:
    """RuntimeError propagates when _chatterbox_model is None."""
    vs = _import_voice_server()
    vs._chatterbox_model = None

    with pytest.raises(RuntimeError, match="TTS model not loaded"):
        vs._synthesize_chunked("test text")
