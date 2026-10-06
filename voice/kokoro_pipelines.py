"""One Kokoro pipeline per language, built when a voice first needs it.

Kokoro's `KPipeline` is constructed for a single `lang_code`, which decides the
grapheme-to-phoneme front end: American English for `a`, British for `b`. The
server used to build exactly one at startup from `KOKORO_LANG`, which made an
accent a property of the deployment — a British voice ran through an American
front end, pronouncing British English as an American would read it, and the
picker hid the British voices rather than fix it.

The voice name carries the language, so the pipeline follows the voice. One
pipeline per language, built on first use and kept: construction loads a model,
and loading it per spoken sentence would put seconds onto every reply.

A name this module cannot read falls back to the configured language rather
than raising. The caller is mid-sentence, and a mispronounced reply beats no
reply at all.
"""

from __future__ import annotations

import logging
import threading

from voice import kokoro_catalogue

log = logging.getLogger(__name__)


def _build(lang_code: str, model=None):  # pragma: no cover - loads a real model
    from kokoro import KPipeline

    # The weights are the same for every language; only the front end differs.
    # Letting each pipeline construct its own `KModel` puts a second copy on a
    # GPU already holding whisper, for nothing.
    if model is None:
        return KPipeline(lang_code=lang_code)
    return KPipeline(lang_code=lang_code, model=model)


class KokoroPipelines:
    """The pipelines this process has built, keyed by language code."""

    def __init__(self, default_language: str = "a", *, factory=_build) -> None:
        self._default = (default_language or "a").strip().lower()[:1] or "a"
        self._factory = factory
        self._pipelines: dict[str, object] = {}
        # Built under a lock: the sample route synthesises in a worker thread,
        # so a Telegram turn can reach an unbuilt language while that thread is
        # inside the factory. Unguarded, both build one — and each unshared
        # build is another model resident on the GPU, surfacing much later as an
        # out-of-memory error on an unrelated voice note.
        self._lock = threading.Lock()

    @property
    def default_language(self) -> str:
        return self._default

    def language_for(self, voice_name: str) -> str:
        """The language code `voice_name` should be spoken through.

        Clamped to the languages this hive offers. A non-English name reaching
        here from an old `KOKORO_VOICE_MAP` or a hand-edited `runtime.yaml` would
        otherwise build a pipeline needing a `misaki` language pack this image
        does not install, raising inside the request on every turn for that Mind
        — where it used to merely mispronounce.
        """
        parsed = kokoro_catalogue.parse_voice_name((voice_name or "").strip())
        if parsed is None or parsed.name[0] not in kokoro_catalogue.ENGLISH_CODES:
            return self._default
        return parsed.name[0]

    def get(self, voice_name: str):
        """The pipeline for `voice_name`, building it on first use."""
        language = self.language_for(voice_name)
        pipeline = self._pipelines.get(language)
        if pipeline is not None:
            return pipeline
        with self._lock:
            pipeline = self._pipelines.get(language)
            if pipeline is None:
                log.info("Loading Kokoro pipeline (lang=%s)...", language)
                pipeline = self._factory(language, self._shared_model())
                self._pipelines[language] = pipeline
        return pipeline

    def _shared_model(self):
        """The model an already-built pipeline holds, for the next to reuse."""
        for pipeline in self._pipelines.values():
            model = getattr(pipeline, "model", None)
            if model is not None:
                return model
        return None

    def warm(self):
        """Build the configured language up front, so no reply waits on a model."""
        return self.get(f"{self._default}f_warm")

    def loaded_languages(self) -> list[str]:
        return sorted(self._pipelines)
