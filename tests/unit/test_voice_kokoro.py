"""Tests for the Kokoro TTS engine path in voice_server.

Verifies that, when TTS_ENGINE=kokoro:
- _resolve_kokoro_voice maps via KOKORO_VOICE_MAP and falls back to the default
- _load_kokoro_voice_map parses JSON and tolerates malformed/empty input
- _synthesize_kokoro drives the voice's own pipeline and concatenates segments
- _synthesize_kokoro raises RuntimeError when the pipeline is not loaded
- _tts_ready / health reflect the Kokoro engine
- the tts() endpoint branches into the Kokoro path

The heavy deps are mocked so the module imports without GPU libs, mirroring
test_voice_tts_logic.py.
"""

import sys
import time
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
    vs._kokoro_loaded = True
    vs._KOKORO_PIPELINES = _FakePipelines(mock_pipeline)

    vs._synthesize_kokoro("Hello Daniel", "af_heart")

    assert vs._KOKORO_PIPELINES.asked_for == ["af_heart"]
    mock_pipeline.assert_called_once_with("Hello Daniel", voice="af_heart")
    # Segments are concatenated into a single 1-D numpy array (not torch.cat) so
    # soundfile can encode the WAV without torchaudio/torchcodec.
    assert np.concatenate.called


def test_synthesize_kokoro_raises_when_not_loaded() -> None:
    vs = _import_voice_server()
    vs._kokoro_loaded = False
    with pytest.raises(RuntimeError, match="TTS model not loaded"):
        vs._synthesize_kokoro("test", "af_heart")


def test_synthesize_kokoro_raises_on_empty_output() -> None:
    vs = _import_voice_server()
    mock_pipeline = MagicMock()
    mock_pipeline.return_value = iter([])
    vs._kokoro_loaded = True
    vs._KOKORO_PIPELINES = _FakePipelines(mock_pipeline)
    with pytest.raises(RuntimeError, match="no audio"):
        vs._synthesize_kokoro("test", "af_heart")


def test_tts_ready_tracks_whether_kokoro_loaded() -> None:
    vs = _import_voice_server()
    vs._kokoro_loaded = False
    assert vs._tts_ready() is False
    vs._kokoro_loaded = True
    assert vs._tts_ready() is True


def test_tts_encodes_the_kokoro_path_through_soundfile() -> None:
    """torchaudio.save routes through torchcodec, which this image omits.

    A torchaudio encode on the Kokoro path 500s every synthesis — the bug that
    silenced Mordecai — so the encode is watched by calling the endpoint rather
    than by reading the source for a function name.
    """
    import asyncio

    vs = _import_voice_server()
    vs._kokoro_loaded = True
    vs._KOKORO_VOICE_MAP = {"ada": "af_bella"}
    vs._KOKORO_PIPELINES = _FakePipelines(
        MagicMock(return_value=iter([("g", "p", MagicMock())]))
    )
    with patch.object(vs, "_wav_to_ogg", return_value=b"OggS-audio"):
        response = asyncio.run(
            vs.tts(vs.TTSRequest(text="Hello Daniel", voice_id="ada"))
        )

    assert response.body == b"OggS-audio"
    assert sys.modules["soundfile"].write.called
    assert not sys.modules["torchaudio"].save.called
    # The voice came from the map, and the pipeline from the voice.
    assert vs._KOKORO_PIPELINES.asked_for == ["af_bella"]


class _FakePipelines:
    """Stands in for the per-language pipeline cache, recording what was asked."""

    def __init__(self, pipeline):
        self._pipeline = pipeline
        self.asked_for: list[str] = []

    def get(self, voice_name: str):
        self.asked_for.append(voice_name)
        return self._pipeline


class TestSamplingAVoice:
    """The Listen button. Grades are not what a voice sounds like."""

    @staticmethod
    def _server():
        vs = _import_voice_server()
        vs._kokoro_loaded = True
        vs._KOKORO_PIPELINES = _FakePipelines(
            MagicMock(return_value=iter([("g", "p", MagicMock())]))
        )
        return vs

    def test_speaks_the_named_voice_and_returns_audio(self):
        import asyncio

        from voice import kokoro_catalogue

        vs = self._server()
        catalogue = kokoro_catalogue.build_catalogue(["voices/bm_george.pt"], "a")
        with patch.object(vs, "_catalogue", side_effect=_async(catalogue)), \
                patch.object(vs, "_wav_to_ogg", return_value=b"OggS-sample"):
            response = asyncio.run(
                vs.voices_sample(vs.VoiceSampleRequest(voice="bm_george"))
            )

        assert response.body == b"OggS-sample"
        assert response.media_type == "audio/ogg"
        assert vs._KOKORO_PIPELINES.asked_for == ["bm_george"]

    def test_refuses_a_voice_the_catalogue_does_not_offer(self):
        """Answering in the default would tell the operator the wrong thing."""
        import asyncio

        from fastapi import HTTPException

        from voice import kokoro_catalogue

        vs = self._server()
        catalogue = kokoro_catalogue.build_catalogue(["voices/bm_george.pt"], "a")
        with patch.object(vs, "_catalogue", side_effect=_async(catalogue)):
            with pytest.raises(HTTPException) as refused:
                asyncio.run(
                    vs.voices_sample(vs.VoiceSampleRequest(voice="zf_xiaobei"))
                )

        assert refused.value.status_code == 400
        assert vs._KOKORO_PIPELINES.asked_for == []

    def test_speaks_a_line_the_caller_supplied(self):
        """The standard line is a default, not the only thing this will say."""
        import asyncio

        from voice import kokoro_catalogue

        vs = self._server()
        catalogue = kokoro_catalogue.build_catalogue(["voices/af_bella.pt"], "a")
        with patch.object(vs, "_catalogue", side_effect=_async(catalogue)), \
                patch.object(vs, "_wav_to_ogg", return_value=b"OggS-sample"), \
                patch.object(vs, "_synthesize_kokoro", return_value=MagicMock()) as synth:
            asyncio.run(
                vs.voices_sample(
                    vs.VoiceSampleRequest(voice="af_bella", text="**Say this** instead")
                )
            )

        # Markdown reaches the speech engine as speech, never as asterisks.
        assert synth.call_args.args == ("Say this instead", "af_bella")

    def test_refuses_while_the_model_is_still_loading(self):
        """503 rather than a 500 out of the synthesiser's own guard."""
        import asyncio

        from fastapi import HTTPException

        vs = self._server()
        vs._kokoro_loaded = False
        with pytest.raises(HTTPException) as refused:
            asyncio.run(vs.voices_sample(vs.VoiceSampleRequest(voice="af_bella")))

        assert refused.value.status_code == 503

    def test_refuses_on_an_engine_that_has_no_catalogue_to_sample(self):
        """Chatterbox clones from a recording; it has no voices to offer."""
        import asyncio

        from fastapi import HTTPException

        vs = self._server()
        vs._TTS_ENGINE = "chatterbox"
        try:
            with pytest.raises(HTTPException) as refused:
                asyncio.run(vs.voices_sample(vs.VoiceSampleRequest(voice="af_bella")))
        finally:
            vs._TTS_ENGINE = "kokoro"

        assert refused.value.status_code == 400

    def test_is_retimed_exactly_as_a_real_reply_is(self):
        """Every reply is retimed before it reaches a surface and no caller
        passes a speed, so a sample at any other tempo is a sample of a voice
        the operator will never hear — while its own words promise otherwise."""
        import asyncio

        from voice import kokoro_catalogue

        vs = self._server()
        catalogue = kokoro_catalogue.build_catalogue(["voices/af_bella.pt"], "a")
        with patch.object(vs, "_catalogue", side_effect=_async(catalogue)), \
                patch.object(vs, "_wav_to_ogg", return_value=b"OggS") as encode:
            asyncio.run(vs.voices_sample(vs.VoiceSampleRequest(voice="af_bella")))

        assert encode.call_args.kwargs["speed"] == vs.TTSRequest(text="x").speed

    def test_speaks_the_standard_sample_line_when_none_is_given(self):
        import asyncio

        from voice import kokoro_catalogue

        vs = self._server()
        catalogue = kokoro_catalogue.build_catalogue(["voices/af_bella.pt"], "a")
        with patch.object(vs, "_catalogue", side_effect=_async(catalogue)), \
                patch.object(vs, "_wav_to_ogg", return_value=b"OggS-sample"), \
                patch.object(vs, "_synthesize_kokoro", return_value=MagicMock()) as synth:
            asyncio.run(vs.voices_sample(vs.VoiceSampleRequest(voice="af_bella")))

        assert synth.call_args.args == (vs.SAMPLE_TEXT, "af_bella")


def _async(value):
    """A stand-in for an awaited call that returns `value`."""

    async def call(*args, **kwargs):
        return value

    return call


class TestStartupWarmsAPipeline:
    """Warming in isolation proves nothing if startup never calls it."""

    @staticmethod
    def _warmed(vs, language: str, default_voice: str) -> list[str]:
        import asyncio

        from voice.kokoro_pipelines import KokoroPipelines

        built: list[str] = []
        vs._KOKORO_DEFAULT_VOICE = default_voice
        vs._KOKORO_PIPELINES = KokoroPipelines(
            language,
            factory=lambda code, model=None: built.append(code) or MagicMock(),
        )
        vs._kokoro_loaded = False
        asyncio.run(vs.startup())
        return built

    def test_the_server_warms_its_configured_language_as_it_starts(self):
        vs = _import_voice_server()
        assert self._warmed(vs, "b", "bm_lewis") == ["b"]
        assert vs._tts_ready() is True

    def test_it_also_warms_the_language_the_default_voice_speaks(self):
        """On this hive the default voice is British and the language American.

        Warming only the configured one left the pipeline every reply actually
        uses to load inside the first reply, on the event loop, while /health
        already said ready.
        """
        vs = _import_voice_server()
        assert self._warmed(vs, "a", "bm_lewis") == ["a", "b"]


class TestTheCatalogueCache:
    # time is imported here because the concurrency case needs a real sleep in
    # the worker thread the fetch runs on.
    """One fetch is two network round trips, and the picker asks on every load."""

    @staticmethod
    def _server():
        vs = _import_voice_server()
        vs._catalogue_cache = None
        return vs

    def test_a_readable_catalogue_is_reused_rather_than_refetched(self):
        import asyncio

        from voice import kokoro_catalogue

        vs = self._server()
        fetched = []

        def fetch(language_code):
            fetched.append(language_code)
            return kokoro_catalogue.build_catalogue(["voices/af_bella.pt"], language_code)

        with patch.object(kokoro_catalogue, "fetch_catalogue", fetch):
            first = asyncio.run(vs._catalogue())
            second = asyncio.run(vs._catalogue())

        assert len(fetched) == 1
        assert second is first

    def test_a_catalogue_whose_card_failed_is_not_remembered(self):
        """Every description empty must not be cached as the card's own silence."""
        import asyncio

        from voice import kokoro_catalogue

        vs = self._server()
        fetched = []

        def fetch(language_code):
            fetched.append(language_code)
            return kokoro_catalogue.build_catalogue(
                ["voices/af_bella.pt"], language_code, "", described=False
            )

        with patch.object(kokoro_catalogue, "fetch_catalogue", fetch):
            asyncio.run(vs._catalogue())
            asyncio.run(vs._catalogue())

        assert len(fetched) == 2

    def test_concurrent_first_readers_share_one_fetch(self):
        """Each fetch is two Hugging Face round trips; the picker asks on load."""
        import asyncio

        from voice import kokoro_catalogue

        vs = self._server()
        fetched = []

        def fetch(language_code):
            fetched.append(language_code)
            time.sleep(0.2)
            return kokoro_catalogue.build_catalogue(["voices/af_bella.pt"], language_code)

        async def scenario():
            return await asyncio.gather(*[vs._catalogue() for _ in range(3)])

        with patch.object(kokoro_catalogue, "fetch_catalogue", fetch):
            answers = asyncio.run(scenario())

        assert len(fetched) == 1
        assert answers[0] is answers[1] is answers[2]

    def test_an_unreadable_catalogue_is_not_remembered(self):
        """The network is what failed; a five-minute memory outlives the blip."""
        import asyncio

        from voice import kokoro_catalogue

        vs = self._server()
        fetched = []

        def fetch(language_code):
            fetched.append(language_code)
            return kokoro_catalogue.unreadable_catalogue(language_code, "refused")

        with patch.object(kokoro_catalogue, "fetch_catalogue", fetch):
            asyncio.run(vs._catalogue())
            asyncio.run(vs._catalogue())

        assert len(fetched) == 2

    def test_a_stale_catalogue_is_refetched_once_its_life_is_up(self):
        import asyncio

        from voice import kokoro_catalogue

        vs = self._server()
        fetched = []

        def fetch(language_code):
            fetched.append(language_code)
            return kokoro_catalogue.build_catalogue(["voices/af_bella.pt"], language_code)

        clock = [1000.0]
        with patch.object(kokoro_catalogue, "fetch_catalogue", fetch), \
                patch.object(vs.time, "monotonic", lambda: clock[0]):
            asyncio.run(vs._catalogue())
            clock[0] += vs._CATALOGUE_TTL_SECONDS - 1
            asyncio.run(vs._catalogue())
            assert len(fetched) == 1
            clock[0] += 2
            asyncio.run(vs._catalogue())

        assert len(fetched) == 2
