"""Tests verifying message queuing infrastructure was cleanly removed from sessions.py."""
import ast
import inspect


def _get_source():
    """Read sessions.py source."""
    from pathlib import Path
    return (Path(__file__).resolve().parents[2] / "core" / "sessions.py").read_text()


def test_no_queues_attribute():
    """_queues dict should not exist in SessionManager."""
    source = _get_source()
    assert "_queues" not in source, "_queues still referenced in sessions.py"


def test_no_busy_attribute():
    """_busy dict should not exist in SessionManager."""
    source = _get_source()
    assert "_busy" not in source, "_busy still referenced in sessions.py"


def test_no_format_queue_batch():
    """_format_queue_batch method should not exist."""
    source = _get_source()
    assert "_format_queue_batch" not in source, "_format_queue_batch still in sessions.py"


def test_no_do_send():
    """_do_send should be inlined back into send_message."""
    source = _get_source()
    assert "_do_send" not in source, "_do_send still in sessions.py"


def test_send_message_exists():
    """send_message must still exist as an async generator."""
    source = _get_source()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "send_message":
            return
    raise AssertionError("send_message async method not found")


def test_syntax_valid():
    """sessions.py must parse without syntax errors."""
    source = _get_source()
    ast.parse(source)  # raises SyntaxError if invalid
