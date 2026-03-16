"""API tests for voice server endpoints.

Verifies:
- /health returns chatterbox engine info when model is loaded
- /health reports loading when model not ready
- /tts returns OGG audio when model is ready
- /tts accepts voice_id parameter
- /tts returns 503 when not ready
- /tts uses chunked synthesis for multi-sentence text
- /tts handles long (200+ word) text and returns audio/ogg
- /stt endpoint unchanged

All heavy deps (torch, numpy, soundfile, chatterbox, faster_whisper) are mocked
so these tests can run in the CI environment without GPU libraries.
"""

import io
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


# Skip all API tests if pydantic can't load (no real FastAPI app available)
pytestmark = pytest.mark.skipif(
    _NEED_PYDANTIC_MOCK,
    reason="pydantic_core native lib unavailable (read-only fs); FastAPI app cannot be created",
)


def test_health_returns_chatterbox_engine(client) -> None:
    """GET /health must return tts_engine=chatterbox when model is loaded."""
    import voice.voice_server as vs
    vs._chatterbox_model = MagicMock()
    vs._whisper = MagicMock()

    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["tts_engine"] == "chatterbox"
    assert data["tts"] == "ready"


def test_health_reports_loading_when_not_ready(client) -> None:
    """GET /health must report tts=loading when model is None."""
    import voice.voice_server as vs
    vs._chatterbox_model = None
    vs._whisper = None

    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["tts"] == "loading"


def test_tts_endpoint_returns_ogg(client, monkeypatch) -> None:
    """POST /tts must return audio/ogg response when model is ready."""
    import voice.voice_server as vs

    # Mock the Chatterbox model
    mock_model = MagicMock()
    mock_model.generate.return_value = MagicMock()  # tensor
    mock_model.sr = 24000
    vs._chatterbox_model = mock_model

    # Mock torchaudio.save to write fake WAV bytes
    torchaudio_mock = sys.modules["torchaudio"]

    def mock_torchaudio_save(buf, wav, sr, format=None):
        buf.write(b"RIFF" + b"\x00" * 100)

    torchaudio_mock.save = mock_torchaudio_save

    # Mock _wav_to_ogg to return fake OGG bytes
    fake_ogg = b"OggS" + b"\x00" * 100
    monkeypatch.setattr(vs, "_wav_to_ogg", lambda wav_bytes, speed=1.0: fake_ogg)

    # Mock _resolve_voice_ref to return a path
    monkeypatch.setattr(vs, "_resolve_voice_ref", lambda vid, vdir=None: "/fake/ref.wav")

    resp = client.post("/tts", json={"text": "Hello Daniel"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/ogg"


def test_tts_endpoint_accepts_voice_id(client, monkeypatch) -> None:
    """POST /tts with voice_id parameter must return 200."""
    import voice.voice_server as vs

    mock_model = MagicMock()
    mock_model.generate.return_value = MagicMock()
    mock_model.sr = 24000
    vs._chatterbox_model = mock_model

    torchaudio_mock = sys.modules["torchaudio"]

    def mock_torchaudio_save(buf, wav, sr, format=None):
        buf.write(b"RIFF" + b"\x00" * 100)

    torchaudio_mock.save = mock_torchaudio_save

    fake_ogg = b"OggS" + b"\x00" * 100
    monkeypatch.setattr(vs, "_wav_to_ogg", lambda wav_bytes, speed=1.0: fake_ogg)
    monkeypatch.setattr(vs, "_resolve_voice_ref", lambda vid, vdir=None: "/fake/ada.wav")

    resp = client.post("/tts", json={"text": "Hello", "voice_id": "ada"})
    assert resp.status_code == 200


def test_tts_endpoint_503_when_not_ready(client) -> None:
    """POST /tts must return 503 when Chatterbox model is not loaded."""
    import voice.voice_server as vs
    vs._chatterbox_model = None

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


def test_tts_endpoint_uses_chunked_synthesis(client, monkeypatch) -> None:
    """POST /tts with multi-sentence text calls _synthesize_chunked and returns 200."""
    import voice.voice_server as vs

    mock_model = MagicMock()
    mock_model.sr = 24000
    vs._chatterbox_model = mock_model

    # Track whether _synthesize_chunked is called
    chunked_called = []

    def fake_synthesize_chunked(text, ref_path=None):
        chunked_called.append(text)
        return MagicMock()

    monkeypatch.setattr(vs, "_synthesize_chunked", fake_synthesize_chunked)

    torchaudio_mock = sys.modules["torchaudio"]

    def mock_torchaudio_save(buf, wav, sr, format=None):
        buf.write(b"RIFF" + b"\x00" * 100)

    torchaudio_mock.save = mock_torchaudio_save

    fake_ogg = b"OggS" + b"\x00" * 100
    monkeypatch.setattr(vs, "_wav_to_ogg", lambda wav_bytes, speed=1.0: fake_ogg)
    monkeypatch.setattr(vs, "_resolve_voice_ref", lambda vid, vdir=None: "/fake/ref.wav")

    resp = client.post("/tts", json={"text": "First sentence. Second sentence."})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/ogg"
    assert len(chunked_called) == 1
    assert chunked_called[0] == "First sentence. Second sentence."


def test_tts_endpoint_long_text_returns_ogg(client, monkeypatch) -> None:
    """POST /tts with 200+ word text returns 200 and audio/ogg through chunked path."""
    import voice.voice_server as vs

    mock_model = MagicMock()
    mock_model.generate.return_value = MagicMock()
    mock_model.sr = 24000
    vs._chatterbox_model = mock_model

    torchaudio_mock = sys.modules["torchaudio"]

    def mock_torchaudio_save(buf, wav, sr, format=None):
        buf.write(b"RIFF" + b"\x00" * 100)

    torchaudio_mock.save = mock_torchaudio_save

    torch_mod = sys.modules["torch"]
    torch_mod.cat.return_value = MagicMock()

    fake_ogg = b"OggS" + b"\x00" * 100
    monkeypatch.setattr(vs, "_wav_to_ogg", lambda wav_bytes, speed=1.0: fake_ogg)
    monkeypatch.setattr(vs, "_resolve_voice_ref", lambda vid, vdir=None: "/fake/ref.wav")

    # Generate a 200+ word text with multiple sentences
    long_text = " ".join(
        f"This is sentence number {i} with some extra words to increase the word count."
        for i in range(25)
    )
    resp = client.post("/tts", json={"text": long_text})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/ogg"
