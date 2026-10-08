"""Which reference clip a chatterbox mind is spoken from.

A mind's directory accumulates candidate recordings; the mind's own record
names the one that is actually spoken. Resolution is deliberately strict: a
clip that is named but absent resolves to nothing, because chatterbox caches
the last reference it was handed and a silent fallback is a reply in a voice
nobody chose.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest


def _can_import(name: str) -> bool:
    try:
        __import__(name)
    except Exception:
        return False
    return True


_NEED_PYDANTIC_MOCK = not _can_import("pydantic")


@pytest.fixture(autouse=True)
def _mock_voice_server_deps(monkeypatch):
    """Mock the GPU stack so voice_server imports on a machine without it."""
    np_mock = MagicMock()
    np_mock.float32 = "float32"
    np_mock.ndarray = type("ndarray", (), {})
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
        monkeypatch.setitem(sys.modules, "fastapi", MagicMock())
        monkeypatch.setitem(sys.modules, "fastapi.responses", MagicMock())

    for mod_name in list(sys.modules):
        if "voice_server" in mod_name:
            del sys.modules[mod_name]
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")


def _import_voice_server():
    with patch("ctypes.CDLL", side_effect=OSError("no GPU")):
        for mod_name in list(sys.modules):
            if "voice_server" in mod_name:
                del sys.modules[mod_name]
        import voice.voice_server as vs

        return vs


@pytest.fixture()
def minds_dir(tmp_path, monkeypatch):
    """A minds directory holding one mind with three candidate clips."""
    vs = _import_voice_server()
    skippy = tmp_path / "skippy"
    skippy.mkdir()
    for name in (
        "voice_ref.wav",
        "_dramitac_mono_voice_ref.wav",
        "dramatic_sterio_voice_ref.wav",
    ):
        (skippy / name).write_bytes(b"RIFF....WAVE")
    (skippy / "runtime.yaml").write_text(
        "mind_id: 14cb820b-4a42-4f04-a593-54f532fd1d2f\nname: skippy\n"
    )
    monkeypatch.setattr(vs, "_MINDS_DIR", str(tmp_path))
    monkeypatch.setattr(vs, "_MIND_ID_TO_NAME", {})
    return vs, skippy


class TestResolvingAClip:
    def test_a_named_clip_resolves_to_that_file(self, minds_dir):
        """Test 7: the clip the mind names, not the conventional one."""
        vs, skippy = minds_dir
        resolved = vs._resolve_voice_ref("skippy", "dramatic_sterio_voice_ref.wav")
        assert resolved == str(skippy / "dramatic_sterio_voice_ref.wav")

    def test_an_underscored_clip_resolves(self, minds_dir):
        vs, skippy = minds_dir
        resolved = vs._resolve_voice_ref("skippy", "_dramitac_mono_voice_ref.wav")
        assert resolved == str(skippy / "_dramitac_mono_voice_ref.wav")

    def test_naming_no_clip_resolves_to_the_conventional_one(self, minds_dir):
        """Test 8: a mind that never picked one sounds as it always did."""
        vs, skippy = minds_dir
        assert vs._resolve_voice_ref("skippy", "") == str(skippy / "voice_ref.wav")
        assert vs._resolve_voice_ref("skippy") == str(skippy / "voice_ref.wav")

    def test_a_named_clip_that_is_gone_resolves_to_nothing(self, minds_dir):
        """Test 9: no silent fallback — chatterbox would reuse a cached ref."""
        vs, _ = minds_dir
        assert vs._resolve_voice_ref("skippy", "deleted_voice_ref.wav") is None

    @pytest.mark.parametrize(
        "clip",
        [
            "../../etc/passwd",
            "..",
            "sub/voice_ref.wav",
            "/etc/passwd",
            ".",
        ],
    )
    def test_a_clip_name_that_is_a_path_resolves_to_nothing(self, minds_dir, clip):
        """Test 10: the name arrives over HTTP and is joined onto a directory."""
        vs, _ = minds_dir
        assert vs._resolve_voice_ref("skippy", clip) is None

    def test_a_mind_id_that_is_a_path_resolves_to_nothing(self, minds_dir):
        vs, _ = minds_dir
        assert vs._resolve_voice_ref("../skippy", "voice_ref.wav") is None

    def test_a_mind_addressed_by_uuid_resolves_the_same_clip(self, minds_dir):
        """The surfaces send a UUID; a person typing into a tool sends a name."""
        vs, skippy = minds_dir
        resolved = vs._resolve_voice_ref(
            "14cb820b-4a42-4f04-a593-54f532fd1d2f", "_dramitac_mono_voice_ref.wav"
        )
        assert resolved == str(skippy / "_dramitac_mono_voice_ref.wav")

    def test_an_unknown_mind_resolves_to_nothing(self, minds_dir):
        vs, _ = minds_dir
        assert vs._resolve_voice_ref("nobody", "voice_ref.wav") is None


class TestTheClipComesFromTheRecord:
    """The mind's record names the clip; this server does not choose one."""

    def _resolver(self, vs, rows):
        from voice.mind_voices import MindVoiceResolver

        return MindVoiceResolver(
            "http://comms:8426", "token", fetch=lambda url, token, timeout: rows
        )

    def test_the_named_clip_is_read_off_the_record(self, minds_dir, monkeypatch):
        vs, _ = minds_dir
        monkeypatch.setattr(
            vs,
            "_MIND_VOICES",
            self._resolver(
                vs, [{"name": "skippy", "voice": "dramatic_sterio_voice_ref.wav"}]
            ),
        )
        assert vs._named_clip("skippy") == "dramatic_sterio_voice_ref.wav"

    def test_a_record_naming_no_clip_yields_none_for_the_fallback(
        self, minds_dir, monkeypatch
    ):
        """Empty, not a guess: resolution turns that into `voice_ref.wav`."""
        vs, _ = minds_dir
        monkeypatch.setattr(
            vs, "_MIND_VOICES", self._resolver(vs, [{"name": "skippy"}])
        )
        assert vs._named_clip("skippy") == ""

    def test_the_record_and_resolution_together_pick_the_file(
        self, minds_dir, monkeypatch
    ):
        vs, skippy = minds_dir
        monkeypatch.setattr(
            vs,
            "_MIND_VOICES",
            self._resolver(
                vs, [{"name": "skippy", "voice": "_dramitac_mono_voice_ref.wav"}]
            ),
        )
        assert vs._resolve_voice_ref("skippy", vs._named_clip("skippy")) == str(
            skippy / "_dramitac_mono_voice_ref.wav"
        )


class TestSamplingAClip:
    def test_the_sample_synthesises_the_named_clip(self, minds_dir, monkeypatch):
        """Test 11: Listen under chatterbox judges a recording by ear."""
        vs, skippy = minds_dir
        handed = {}

        def fake_chunked(text, ref_path=None):
            handed["text"] = text
            handed["ref_path"] = ref_path
            return "waveform"

        monkeypatch.setattr(vs, "_synthesize_chunked", fake_chunked)
        monkeypatch.setattr(vs, "_wav_to_ogg", lambda wav, speed=1.0: b"ogg-bytes")
        monkeypatch.setattr(vs, "torchaudio", MagicMock())
        model = MagicMock()
        model.sr = 24000
        monkeypatch.setattr(vs, "_chatterbox_model", model)

        produced = vs._clip_sample_bytes(
            "Hey there.", str(skippy / "_dramitac_mono_voice_ref.wav"), 0.9
        )
        assert produced == b"ogg-bytes"
        assert handed["ref_path"] == str(skippy / "_dramitac_mono_voice_ref.wav")
        assert handed["text"] == "Hey there."

    def test_a_clip_the_mind_does_not_have_is_not_sampled(self, minds_dir):
        """Test 12: a sample of the wrong recording is worse than no sample."""
        vs, _ = minds_dir
        assert vs._resolve_voice_ref("skippy", "someone_elses.wav") is None
