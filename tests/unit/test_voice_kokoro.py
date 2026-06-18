"""Tests for the Kokoro TTS engine path in voice_server.

Verifies that, when TTS_ENGINE=kokoro:
- _resolve_kokoro_voice maps via KOKORO_VOICE_MAP and falls back to the default
- _load_kokoro_voice_map parses JSON and tolerates malformed/empty input
- _synthesize_kokoro drives the pipeline and concatenates segments
- _synthesize_kokoro raises RuntimeError when the pipeline is not loaded
- _tts_ready / health reflect the Kokoro engine
- the tts() endpoint branches into the Kokoro path

The heavy deps are mocked so the module imports without GPU libs, mirroring
test_voice_tts_logic.py.
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
    """Mock heavy deps and force the Kokoro engine before import."""
    np_mock = MagicMock()
    np_mock.float32 = "float32"
    np_mock.ndarray = type("ndarray", (), {})
    monkeypatch.setitem(sys.modules, "numpy", np_mock)

    torch_mock = MagicMock()
    torch_mock.cuda.is_available.return_value = False
    torch_mock.is_tensor.return_value = True
    monkeypatch.setitem(sys.modules, "torch", torch_mock)

    monkeypatch.setitem(sys.modules, "torchaudio", MagicMock())
    monkeypatch.setitem(sys.modules, "faster_whisper", MagicMock())
    monkeypatch.setitem(sys.modules, "soundfile", MagicMock())

    # Mock the kokoro package so importing/loading never touches real weights.
    kokoro_mod = MagicMock()
    monkeypatch.setitem(sys.modules, "kokoro", kokoro_mod)

    if _NEED_PYDANTIC_MOCK:
        pydantic_mock = MagicMock()
        pydantic_mock.BaseModel = type("BaseModel", (), {})
        monkeypatch.setitem(sys.modules, "pydantic", pydantic_mock)
        monkeypatch.setitem(sys.modules, "pydantic_core", MagicMock())
        fastapi_mock = MagicMock()
        monkeypatch.setitem(sys.modules, "fastapi", fastapi_mock)
        monkeypatch.setitem(sys.modules, "fastapi.responses", MagicMock())

    # Force the Kokoro engine — _TTS_ENGINE is read at import time.
    monkeypatch.setenv("TTS_ENGINE", "kokoro")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")

    for mod_name in list(sys.modules.keys()):
        if "voice_server" in mod_name:
            del sys.modules[mod_name]


def _import_voice_server():
    with patch("ctypes.CDLL", side_effect=OSError("no GPU")):
        for mod_name in list(sys.modules.keys()):
            if "voice_server" in mod_name:
                del sys.modules[mod_name]
        import voice.voice_server as vs
        return vs


def test_engine_is_kokoro() -> None:
    vs = _import_voice_server()
    assert vs._TTS_ENGINE == "kokoro"


def test_resolve_kokoro_voice_uses_map() -> None:
    vs = _import_voice_server()
    vs._KOKORO_VOICE_MAP = {"ada": "af_bella"}
    assert vs._resolve_kokoro_voice("ada") == "af_bella"


def test_resolve_kokoro_voice_falls_back_to_default() -> None:
    vs = _import_voice_server()
    vs._KOKORO_VOICE_MAP = {}
    assert vs._resolve_kokoro_voice("unknown") == vs._KOKORO_DEFAULT_VOICE


def test_load_kokoro_voice_map_parses_json(monkeypatch) -> None:
    vs = _import_voice_server()
    monkeypatch.setenv("KOKORO_VOICE_MAP", '{"ada": "af_bella", "bob": "am_michael"}')
    assert vs._load_kokoro_voice_map() == {"ada": "af_bella", "bob": "am_michael"}


def test_load_kokoro_voice_map_empty_when_unset(monkeypatch) -> None:
    vs = _import_voice_server()
    monkeypatch.delenv("KOKORO_VOICE_MAP", raising=False)
    assert vs._load_kokoro_voice_map() == {}


def test_load_kokoro_voice_map_ignores_malformed(monkeypatch) -> None:
    vs = _import_voice_server()
    monkeypatch.setenv("KOKORO_VOICE_MAP", "not json {")
    assert vs._load_kokoro_voice_map() == {}


def test_load_kokoro_voice_map_ignores_non_object(monkeypatch) -> None:
    vs = _import_voice_server()
    monkeypatch.setenv("KOKORO_VOICE_MAP", '["af_heart"]')
    assert vs._load_kokoro_voice_map() == {}


def test_synthesize_kokoro_drives_pipeline_and_concats() -> None:
    vs = _import_voice_server()
    import numpy as np

    mock_pipeline = MagicMock()
    # Kokoro yields (graphemes, phonemes, audio) tuples
    mock_pipeline.return_value = iter([("g1", "p1", MagicMock()), ("g2", "p2", MagicMock())])
    vs._kokoro_pipeline = mock_pipeline

    vs._synthesize_kokoro("Hello Daniel", "af_heart")

    mock_pipeline.assert_called_once_with("Hello Daniel", voice="af_heart")
    # Segments are concatenated into a single 1-D numpy array (not torch.cat) so
    # soundfile can encode the WAV without torchaudio/torchcodec.
    assert np.concatenate.called


def test_synthesize_kokoro_raises_when_not_loaded() -> None:
    vs = _import_voice_server()
    vs._kokoro_pipeline = None
    with pytest.raises(RuntimeError, match="TTS model not loaded"):
        vs._synthesize_kokoro("test", "af_heart")


def test_synthesize_kokoro_raises_on_empty_output() -> None:
    vs = _import_voice_server()
    mock_pipeline = MagicMock()
    mock_pipeline.return_value = iter([])
    vs._kokoro_pipeline = mock_pipeline
    with pytest.raises(RuntimeError, match="no audio"):
        vs._synthesize_kokoro("test", "af_heart")


def test_tts_ready_tracks_kokoro_pipeline() -> None:
    vs = _import_voice_server()
    vs._kokoro_pipeline = None
    assert vs._tts_ready() is False
    vs._kokoro_pipeline = MagicMock()
    assert vs._tts_ready() is True


def test_tts_endpoint_branches_to_kokoro() -> None:
    """The tts() endpoint must contain the Kokoro branch."""
    import pathlib

    source_path = pathlib.Path(__file__).resolve().parents[2] / "voice" / "voice_server.py"
    source = source_path.read_text()
    lines = source.splitlines()
    in_tts = False
    tts_body: list[str] = []
    for line in lines:
        if "async def tts(" in line:
            in_tts = True
            continue
        if in_tts:
            if line and not line.startswith(" ") and not line.startswith("\t"):
                break
            tts_body.append(line)
    tts_source = "\n".join(tts_body)

    assert '_TTS_ENGINE == "kokoro"' in tts_source
    assert "_synthesize_kokoro(" in tts_source
    assert "_resolve_kokoro_voice(" in tts_source
    # The Kokoro WAV encode must go through soundfile, not torchaudio.save:
    # torchaudio's save() routes through torchcodec, which the slim Kokoro
    # image deliberately does not ship. A torchaudio.save on the Kokoro path
    # 500s every synthesis (the bug that silenced Mordecai).
    assert "sf.write(" in tts_source
    assert "import soundfile" in tts_source
