"""Which front end a voice is spoken through.

Kokoro builds a pipeline for one language code, and that code decides the
grapheme-to-phoneme front end. The server used to build exactly one from
`KOKORO_LANG`, which made the accent a property of the deployment; the voice
name carries the language, so the pipeline follows the voice.
"""

from __future__ import annotations

from voice.kokoro_pipelines import KokoroPipelines


class Recorder:
    """Stands in for `KPipeline`, which loads a model. Records what it built."""

    def __init__(self):
        self.built: list[str] = []

    def __call__(self, lang_code: str):
        self.built.append(lang_code)
        return f"pipeline:{lang_code}"


class TestThePipelineFollowsTheVoice:
    def test_a_british_voice_is_spoken_through_a_british_front_end(self):
        """On a server configured for American English, which is this one."""
        factory = Recorder()
        pipelines = KokoroPipelines("a", factory=factory)
        assert pipelines.get("bm_lewis") == "pipeline:b"
        assert factory.built == ["b"]

    def test_an_american_voice_is_spoken_through_the_american_front_end(self):
        factory = Recorder()
        pipelines = KokoroPipelines("a", factory=factory)
        assert pipelines.get("af_bella") == "pipeline:a"
        assert factory.built == ["a"]

    def test_both_accents_coexist_in_one_process(self):
        factory = Recorder()
        pipelines = KokoroPipelines("a", factory=factory)
        pipelines.get("af_bella")
        pipelines.get("bf_emma")
        assert factory.built == ["a", "b"]
        assert pipelines.loaded_languages() == ["a", "b"]


class TestLoadingOnceIsTheWholePoint:
    def test_the_same_language_is_built_once_however_often_it_is_asked_for(self):
        """Construction loads a model; per sentence it would be seconds a turn."""
        factory = Recorder()
        pipelines = KokoroPipelines("a", factory=factory)
        for name in ("af_bella", "af_heart", "am_michael"):
            pipelines.get(name)
        assert factory.built == ["a"]


class TestANameItCannotRead:
    def test_falls_back_to_the_configured_language_rather_than_raising(self):
        """The caller is mid-sentence. A mispronounced reply beats no reply."""
        factory = Recorder()
        pipelines = KokoroPipelines("b", factory=factory)
        assert pipelines.get("not-a-voice") == "pipeline:b"
        assert pipelines.language_for("") == "b"
        assert factory.built == ["b"]

    def test_a_voice_in_a_language_kokoro_does_not_have_falls_back_too(self):
        factory = Recorder()
        pipelines = KokoroPipelines("a", factory=factory)
        assert pipelines.language_for("qf_nobody") == "a"


class TestWarming:
    def test_warms_the_configured_language_so_the_first_reply_is_not_the_slow_one(self):
        factory = Recorder()
        pipelines = KokoroPipelines("b", factory=factory)
        pipelines.warm()
        assert factory.built == ["b"]
        assert pipelines.default_language == "b"
