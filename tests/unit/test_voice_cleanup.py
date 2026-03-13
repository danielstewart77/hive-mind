"""Tests for voice cleanup -- fish_tokenizer removal (Step 4).

Verifies that:
- fish_tokenizer.py no longer exists
- voice_server.py does not import fish_tokenizer
"""

import os

FISH_TOKENIZER_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "voice", "fish_tokenizer.py"
)
VOICE_SERVER_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "voice", "voice_server.py"
)


def test_fish_tokenizer_removed() -> None:
    """voice/fish_tokenizer.py must not exist."""
    assert not os.path.exists(FISH_TOKENIZER_PATH), (
        "fish_tokenizer.py should be deleted"
    )


def test_no_fish_imports_in_voice_server() -> None:
    """voice_server.py must not import fish_tokenizer."""
    with open(VOICE_SERVER_PATH) as f:
        source = f.read()
    assert "fish_tokenizer" not in source, (
        "voice_server.py still imports fish_tokenizer"
    )
