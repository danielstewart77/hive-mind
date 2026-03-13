"""API tests for voice server endpoints (Step 2).

Verifies:
- /health returns xtts_v2 engine info
- /health reports loading when model not ready
- /tts returns OGG audio
- /tts returns 503 when not ready
- /stt endpoint unchanged

All heavy deps (torch, numpy, soundfile, TTS, faster_whisper) are mocked
so these tests can run in the CI environment without GPU libraries.
"""

import io
import sys
import types
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_voice_deps(monkeypatch):
    """Mock all heavy deps before importing voice_server."""
    # Mock numpy
    np_mock = MagicMock()
    np_mock.float32 = "float32"
    np_mock.ndarray = type("ndarray", (), {})
    np_mock.array = MagicMock(return_value=MagicMock())
    np_mock.zeros = MagicMock(return_value=MagicMock())
    monkeypatch.setitem(sys.modules, "numpy", np_mock)

    # Mock torch
    torch_mock = MagicMock()
    torch_mock.cuda.is_available.return_value = False
    monkeypatch.setitem(sys.modules, "torch", torch_mock)

    # Mock torchaudio
    monkeypatch.setitem(sys.modules, "torchaudio", MagicMock())

    # Mock faster_whisper
    fw_mock = MagicMock()
    monkeypatch.setitem(sys.modules, "faster_whisper", fw_mock)

    # Mock soundfile
    sf_mock = MagicMock()
    monkeypatch.setitem(sys.modules, "soundfile", sf_mock)

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


@pytest.fixture
def voice_app(monkeypatch):
    """Import the voice server app with all deps mocked."""
    with patch("ctypes.CDLL", side_effect=OSError("no GPU")):
        # Need to remove cached module to reimport
        for mod_name in list(sys.modules.keys()):
            if "voice_server" in mod_name or "voice.voice_server" in mod_name:
                del sys.modules[mod_name]

        from voice.voice_server import app
        return app


@pytest.fixture
def client(voice_app):
    """Create a TestClient for the voice server."""
    from starlette.testclient import TestClient
    return TestClient(voice_app, raise_server_exceptions=False)


def test_health_returns_xtts_engine(client) -> None:
    """GET /health must return tts_engine=xtts_v2 when model is loaded."""
    import voice.voice_server as vs
    vs._xtts_model = MagicMock()  # Simulate loaded model
    vs._use_voice_cloning = True
    vs._whisper = MagicMock()

    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["tts_engine"] == "xtts_v2"
    assert data["tts"] == "ready"


def test_health_reports_loading_when_not_ready(client) -> None:
    """GET /health must report tts=loading when model is None."""
    import voice.voice_server as vs
    vs._xtts_model = None
    vs._whisper = None

    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["tts"] == "loading"


def test_tts_endpoint_returns_ogg(client, monkeypatch) -> None:
    """POST /tts must return audio/ogg response when model is ready."""
    import voice.voice_server as vs

    # Mock the XTTS model
    mock_model = MagicMock()
    mock_model.tts.return_value = [0.0] * 16000  # simulated audio samples
    vs._xtts_model = mock_model
    vs._use_voice_cloning = False

    # Mock _wav_to_ogg to return fake OGG bytes
    fake_ogg = b"OggS" + b"\x00" * 100
    monkeypatch.setattr(vs, "_wav_to_ogg", lambda wav_bytes, speed=1.0: fake_ogg)

    # Mock soundfile.write to produce valid WAV
    def mock_sf_write(file, data, samplerate, format=None):
        """Write minimal data to the buffer."""
        if hasattr(file, 'write'):
            file.write(b"RIFF" + b"\x00" * 100)

    monkeypatch.setattr(vs.sf, "write", mock_sf_write)

    # Mock np.array to return something
    np_mock = sys.modules["numpy"]
    np_mock.array.return_value = [0.0] * 16000

    resp = client.post("/tts", json={"text": "Hello Daniel"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/ogg"


def test_tts_endpoint_503_when_not_ready(client) -> None:
    """POST /tts must return 503 when XTTS model is not loaded."""
    import voice.voice_server as vs
    vs._xtts_model = None

    resp = client.post("/tts", json={"text": "Hello"})
    assert resp.status_code == 503


def test_stt_endpoint_unchanged(client, monkeypatch) -> None:
    """POST /stt must still work (Whisper STT is not affected by TTS changes)."""
    import voice.voice_server as vs
    mock_whisper = MagicMock()

    # Mock segments
    mock_segment = MagicMock()
    mock_segment.text = "Hello world"
    mock_whisper.transcribe.return_value = ([mock_segment], None)
    vs._whisper = mock_whisper

    # Mock _ogg_to_wav
    monkeypatch.setattr(vs, "_ogg_to_wav", lambda x: b"RIFF" + b"\x00" * 100)

    # Create a fake audio file upload
    fake_audio = io.BytesIO(b"fake audio data")

    resp = client.post(
        "/stt",
        files={"file": ("test.ogg", fake_audio, "audio/ogg")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "text" in data
