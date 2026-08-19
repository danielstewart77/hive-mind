"""Whisper's silence hallucinations -- the defences, one test per requirement."""

from voice.stt_filters import TRANSCRIBE_OPTS, clean_transcript, is_hallucination


def test_non_speech_audio_never_reaches_the_decoder():
    """Silence is dropped by VAD before Whisper can caption it."""
    assert TRANSCRIBE_OPTS["vad_filter"] is True


def test_a_hallucination_cannot_seed_the_next_one():
    """Each window decodes independently, so one stray phrase can't loop."""
    assert TRANSCRIBE_OPTS["condition_on_previous_text"] is False


def test_text_spanning_a_silent_gap_is_flagged():
    """faster-whisper's own gap detector is armed, with the timestamps it needs."""
    assert TRANSCRIBE_OPTS["hallucination_silence_threshold"] == 2.0
    assert TRANSCRIBE_OPTS["word_timestamps"] is True


def test_a_segment_that_is_only_filler_is_dropped():
    assert is_hallucination(" Thanks for watching! ")
    assert is_hallucination("Thank you.")
    assert is_hallucination("[Music]")
    assert clean_transcript(["Restart the service.", " Thank you."]) == "Restart the service."


def test_a_real_sentence_containing_filler_is_kept_whole():
    line = "Thank you for watching the logs while I restart it."
    assert not is_hallucination(line)
    assert clean_transcript([line]) == line
