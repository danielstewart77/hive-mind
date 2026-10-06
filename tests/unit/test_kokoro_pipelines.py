"""Which front end a voice is spoken through.

Kokoro builds a pipeline for one language code, and that code decides the
grapheme-to-phoneme front end. The server used to build exactly one from
`KOKORO_LANG`, which made the accent a property of the deployment; the voice
name carries the language, so the pipeline follows the voice.
"""

from __future__ import annotations

import threading
import time

from voice.kokoro_pipelines import KokoroPipelines


class Recorder:
    """Stands in for `KPipeline`, which loads a model. Records what it built."""

    def __init__(self, delay: float = 0.0):
        self.built: list[str] = []
        self.models: list[object] = []
        self._delay = delay

    def __call__(self, lang_code: str, model=None):
        self.built.append(lang_code)
        self.models.append(model)
        if self._delay:
            time.sleep(self._delay)
        return _Pipeline(lang_code)


class _Pipeline:
    """What `KPipeline` is, for this module's purposes: a thing with a model."""

    def __init__(self, lang_code: str):
        self.lang_code = lang_code
        self.model = f"model:{lang_code}"

    def __eq__(self, other):
        return isinstance(other, _Pipeline) and other.lang_code == self.lang_code


class TestThePipelineFollowsTheVoice:
    def test_a_british_voice_is_spoken_through_a_british_front_end(self):
        """On a server configured for American English, which is this one."""
        factory = Recorder()
        pipelines = KokoroPipelines("a", factory=factory)
        assert pipelines.get("bm_lewis").lang_code == "b"
        assert factory.built == ["b"]

    def test_an_american_voice_is_spoken_through_the_american_front_end(self):
        factory = Recorder()
        pipelines = KokoroPipelines("a", factory=factory)
        assert pipelines.get("af_bella").lang_code == "a"
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
        assert pipelines.get("not-a-voice").lang_code == "b"
        assert pipelines.language_for("") == "b"
        assert factory.built == ["b"]

    def test_a_voice_in_a_language_kokoro_does_not_have_falls_back_too(self):
        factory = Recorder()
        pipelines = KokoroPipelines("a", factory=factory)
        assert pipelines.language_for("qf_nobody") == "a"

    def test_a_language_this_hive_does_not_offer_falls_back_too(self):
        """Kokoro has it; this image has no `misaki` pack for it.

        A name like this reaches here from an old `KOKORO_VOICE_MAP` or a
        hand-edited runtime.yaml. Building its pipeline raises inside the
        request, on every turn for that Mind, where it used to mispronounce.
        """
        factory = Recorder()
        pipelines = KokoroPipelines("a", factory=factory)
        assert pipelines.language_for("zf_xiaobei") == "a"
        assert pipelines.language_for("jf_alpha") == "a"
        assert factory.built == []


class TestOneModelBetweenThem:
    def test_the_second_accent_reuses_the_first_pipelines_model(self):
        """Both accents are the same weights behind a different front end.

        A second `KModel` on a GPU already holding whisper buys nothing and
        surfaces later as an out-of-memory error on an unrelated voice note.
        """
        factory = Recorder()
        pipelines = KokoroPipelines("a", factory=factory)
        pipelines.get("af_bella")
        pipelines.get("bm_lewis")
        assert factory.models == [None, "model:a"]


class TestConcurrentFirstUse:
    def test_one_language_is_built_once_even_from_several_threads(self):
        """The sample route synthesises in a worker thread, so a Telegram turn
        can reach an unbuilt language while that thread is inside the factory."""
        factory = Recorder(delay=0.2)
        pipelines = KokoroPipelines("a", factory=factory)
        threads = [
            threading.Thread(target=pipelines.get, args=("bm_lewis",))
            for _ in range(3)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert factory.built == ["b"]


class TestWarming:
    def test_warms_the_configured_language_so_the_first_reply_is_not_the_slow_one(self):
        factory = Recorder()
        pipelines = KokoroPipelines("b", factory=factory)
        pipelines.warm()
        assert factory.built == ["b"]
        assert pipelines.default_language == "b"
