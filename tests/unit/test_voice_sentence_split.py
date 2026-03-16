"""Tests for _split_sentences() in voice_server.

Verifies sentence-boundary splitting:
- Single and multiple sentences
- Abbreviation preservation (Dr., Mr., etc.)
- Ellipsis handling
- Empty and whitespace-only input
- Inputs without terminal punctuation
- Long multi-sentence texts
"""

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
def _mock_voice_server_deps(monkeypatch):
    """Mock heavy deps so voice_server can be imported without GPU libs."""
    np_mock = MagicMock()
    np_mock.float32 = "float32"
    np_mock.ndarray = type("ndarray", (), {})
    np_mock.array = MagicMock(side_effect=lambda x, dtype=None: x)
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

        fastapi_mock = MagicMock()
        monkeypatch.setitem(sys.modules, "fastapi", fastapi_mock)
        monkeypatch.setitem(sys.modules, "fastapi.responses", MagicMock())

    for mod_name in list(sys.modules.keys()):
        if "voice_server" in mod_name or "voice.voice_server" in mod_name:
            del sys.modules[mod_name]

    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")


def _import_voice_server():
    """Import voice_server with GPU check bypassed."""
    with patch("ctypes.CDLL", side_effect=OSError("no GPU")):
        for mod_name in list(sys.modules.keys()):
            if "voice_server" in mod_name or "voice.voice_server" in mod_name:
                del sys.modules[mod_name]
        import voice.voice_server as vs
        return vs


def test_split_single_sentence() -> None:
    """Single sentence returns a list with one item."""
    vs = _import_voice_server()
    result = vs._split_sentences("Hello world.")
    assert result == ["Hello world."]


def test_split_multiple_sentences() -> None:
    """Multiple sentences separated by punctuation + space are split correctly."""
    vs = _import_voice_server()
    result = vs._split_sentences("First sentence. Second sentence! Third?")
    assert len(result) == 3
    assert result[0] == "First sentence."
    assert result[1] == "Second sentence!"
    assert result[2] == "Third?"


def test_split_preserves_abbreviations() -> None:
    """Common abbreviations (Dr., Mr., etc.) should NOT cause a split."""
    vs = _import_voice_server()
    result = vs._split_sentences("Dr. Smith went home. He was tired.")
    assert len(result) == 2
    assert result[0] == "Dr. Smith went home."
    assert result[1] == "He was tired."


def test_split_handles_ellipsis() -> None:
    """Ellipsis (...) should not produce extra empty splits."""
    vs = _import_voice_server()
    result = vs._split_sentences("Wait... What happened? I see.")
    # Should not have empty strings in the result
    assert all(s.strip() for s in result)
    # Should produce reasonable chunks (ellipsis is part of first sentence)
    assert len(result) >= 2


def test_split_empty_string() -> None:
    """Empty string returns a list with one empty string (not an empty list)."""
    vs = _import_voice_server()
    result = vs._split_sentences("")
    assert result == [""]


def test_split_no_terminal_punctuation() -> None:
    """Text without sentence-ending punctuation returns as a single chunk."""
    vs = _import_voice_server()
    result = vs._split_sentences("No period at the end")
    assert result == ["No period at the end"]


def test_split_whitespace_only() -> None:
    """Whitespace-only input is passed through as a single chunk."""
    vs = _import_voice_server()
    result = vs._split_sentences("   ")
    assert result == ["   "]


def test_split_preserves_eg_abbreviation() -> None:
    """e.g. should NOT cause a sentence split."""
    vs = _import_voice_server()
    result = vs._split_sentences("Use this, e.g. bananas. That is it.")
    assert len(result) == 2
    assert result[0] == "Use this, e.g. bananas."
    assert result[1] == "That is it."


def test_split_preserves_ie_abbreviation() -> None:
    """i.e. should NOT cause a sentence split."""
    vs = _import_voice_server()
    result = vs._split_sentences("The main one, i.e. the first. Done.")
    assert len(result) == 2
    assert result[0] == "The main one, i.e. the first."
    assert result[1] == "Done."


def test_split_long_text_produces_multiple_chunks() -> None:
    """A text with 10+ sentences should produce 10+ chunks."""
    vs = _import_voice_server()
    sentences = [f"Sentence number {i}." for i in range(12)]
    text = " ".join(sentences)
    result = vs._split_sentences(text)
    assert len(result) >= 10
