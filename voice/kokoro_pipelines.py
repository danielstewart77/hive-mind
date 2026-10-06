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

from voice import kokoro_catalogue

log = logging.getLogger(__name__)


def _build(lang_code: str):  # pragma: no cover - loads a real model
    from kokoro import KPipeline

    return KPipeline(lang_code=lang_code)


class KokoroPipelines:
    """The pipelines this process has built, keyed by language code."""

    def __init__(self, default_language: str = "a", *, factory=_build) -> None:
        self._default = (default_language or "a").strip().lower()[:1] or "a"
        self._factory = factory
        self._pipelines: dict[str, object] = {}

    @property
    def default_language(self) -> str:
        return self._default

    def language_for(self, voice_name: str) -> str:
        """The language code `voice_name` should be spoken through."""
        parsed = kokoro_catalogue.parse_voice_name((voice_name or "").strip())
        if parsed is None:
            return self._default
        return parsed.name[0]

    def get(self, voice_name: str):
        """The pipeline for `voice_name`, building it on first use."""
        language = self.language_for(voice_name)
        pipeline = self._pipelines.get(language)
        if pipeline is None:
            log.info("Loading Kokoro pipeline (lang=%s)...", language)
            pipeline = self._factory(language)
            self._pipelines[language] = pipeline
        return pipeline

    def warm(self, language: str | None = None):
        """Build one pipeline up front so the first reply is not the slow one."""
        code = (language or self._default).strip().lower()[:1] or self._default
        return self.get(f"{code}f_warm")

    def loaded_languages(self) -> list[str]:
        return sorted(self._pipelines)
