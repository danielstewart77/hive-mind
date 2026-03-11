"""Tests verifying lock.locked() guards are present in telegram_bot.py handlers.

These guards provide user feedback when a message is already being processed,
preventing messages from silently queuing behind the lock.
"""
import ast


def _get_source():
    from pathlib import Path
    return (Path(__file__).resolve().parents[2] / "clients" / "telegram_bot.py").read_text()


def test_lock_locked_guards_present():
    """lock.locked() early-exit guards must be present in all four handlers."""
    source = _get_source()
    assert source.count("lock.locked()") >= 4, (
        "Expected at least 4 lock.locked() guards (handle_text, handle_photo, "
        "handle_voice, handle_unknown_command)"
    )


def test_still_processing_message_present():
    """The 'Still processing' user-facing feedback must be present."""
    source = _get_source()
    assert "Still processing" in source, (
        "'Still processing' message not found in telegram_bot.py"
    )


def test_async_with_lock_blocks_remain():
    """All four handlers must still have 'async with lock:' blocks."""
    source = _get_source()
    count = source.count("async with lock:")
    # handle_text, handle_photo, handle_voice, handle_unknown_command, cmd_skill = 5
    assert count >= 4, f"Expected at least 4 'async with lock:' blocks, found {count}"


def test_syntax_valid():
    """telegram_bot.py must parse without syntax errors."""
    source = _get_source()
    ast.parse(source)
