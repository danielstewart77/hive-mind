"""Tests verifying sessions.py structural requirements."""
import ast


def _get_source():
    """Read sessions.py source."""
    from pathlib import Path
    return (Path(__file__).resolve().parents[2] / "core" / "sessions.py").read_text()


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
