"""
Hive Mind -- STT hallucination defences.

Whisper was trained largely on YouTube captions, so a chunk of near-silence
does not decode to nothing: it decodes to the most probable thing said at the
end of a video. "Thank you." "Thanks for watching." "Please subscribe."
Daniel's dictation is full of pauses, so every pause was minting one.

Three defences, in descending order of how much they actually help:

1. ``vad_filter`` -- Silero VAD, bundled with faster-whisper, drops non-speech
   audio before the decoder ever sees it. This is the fix; the rest is cleanup.
2. ``condition_on_previous_text=False`` -- a hallucination cannot seed the
   next window, which is what turns one stray phrase into a repeating loop.
3. ``hallucination_silence_threshold`` -- faster-whisper's own detector for
   text emitted across a silent gap. It needs word timestamps to find the gap.

Anything that still slips through is caught by phrase matching, applied per
*segment* rather than to the joined transcript: a segment that is nothing but
a known filler phrase is dropped, and one that merely contains the phrase is
kept whole. Matching the joined text would let "thanks for watching" eat a
sentence that legitimately contained those words.

This module deliberately imports nothing heavy -- voice_server pulls in torch
and torchaudio at import time, and the decisions here are testable without a
GPU or a model on disk.
"""

import re

# faster-whisper's log_prob_threshold (-1.0) and no_speech_threshold (0.6)
# defaults are already the recommended values, so they are not restated here.
TRANSCRIBE_OPTS: dict = {
    "language": "en",
    "vad_filter": True,
    "vad_parameters": {"min_silence_duration_ms": 500, "speech_pad_ms": 200},
    "condition_on_previous_text": False,
    "word_timestamps": True,
    "hallucination_silence_threshold": 2.0,
}

# Segments equal to one of these -- after case-folding and stripping
# punctuation -- are Whisper's YouTube-caption reflex, not speech.
_FILLER_PHRASES = frozenset({
    "thank you",
    "thanks",
    "thank you very much",
    "thanks for watching",
    "thank you for watching",
    "thanks for listening",
    "thank you for listening",
    "subscribe",
    "please subscribe",
    "like and subscribe",
    "subscribe to my channel",
    "dont forget to subscribe",
    "see you next time",
    "see you in the next video",
    "bye",
    "bye bye",
    "goodbye",
    "you",
    "amen",
})

# Bracketed sound tags and bare musical notes: [Music], (upbeat music), ...
_TAG_ONLY = re.compile(r"^[\s\W_]*$|^[\[\(].*[\]\)]$")

_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def _normalise(text: str) -> str:
    """Case-fold, drop punctuation, collapse whitespace."""
    return _WS.sub(" ", _PUNCT.sub("", text.casefold())).strip()


def is_hallucination(segment_text: str) -> bool:
    """True if this segment is entirely a known silence artefact."""
    stripped = segment_text.strip()
    if _TAG_ONLY.match(stripped):
        return True
    return _normalise(stripped) in _FILLER_PHRASES


def clean_transcript(segment_texts) -> str:
    """Join surviving segments into a transcript, dropping the artefacts."""
    return " ".join(
        t.strip() for t in segment_texts if not is_hallucination(t)
    ).strip()
