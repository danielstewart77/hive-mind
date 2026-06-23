"""Structural guards for the collapsed mind-template set.

The eight provider-specific templates were reconciled down to two
harness-generic ones — `claude_cli` (built from Ada, Claude or Ollama via
runtime env) and `codex_cli` (built from Bilby with Nagatha's kill
hardening, OpenAI or Ollama via runtime `_provider_args`). create-mind
selects one of these by harness alone and substitutes the literal
`MIND_NAME` token at scaffold time. These tests pin that contract so a
future edit can't reintroduce a stale provider split, leave a template
unparseable, or bake a live mind's name into a generated mind.
"""

from __future__ import annotations

import ast
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "mind_templates"

EXPECTED = {"claude_cli.py", "codex_cli.py"}
REMOVED = {
    "claude_cli_claude.py",
    "claude_cli_ollama.py",
    "claude_sdk_claude.py",
    "claude_sdk_ollama.py",
    "codex_cli_codex.py",
    "codex_cli_ollama.py",
    "codex_sdk_codex.py",
    "codex_sdk_ollama.py",
}


def test_template_set_is_exactly_the_two_harness_templates() -> None:
    present = {p.name for p in TEMPLATES_DIR.glob("*.py")}
    assert present == EXPECTED, f"unexpected template set: {present}"


def test_old_provider_split_templates_are_gone() -> None:
    for name in REMOVED:
        assert not (TEMPLATES_DIR / name).exists(), f"stale template survived: {name}"


def test_both_templates_parse() -> None:
    for name in EXPECTED:
        source = (TEMPLATES_DIR / name).read_text()
        ast.parse(source)  # raises SyntaxError on regression


def test_logger_is_genericised_to_placeholder() -> None:
    # The logger name is the one spot a live mind's identity would leak into
    # every scaffolded mind. create-mind sed-substitutes MIND_NAME, so the
    # template must carry the placeholder, not "ada"/"bilby"/etc.
    for name in EXPECTED:
        source = (TEMPLATES_DIR / name).read_text()
        assert 'logging.getLogger("hive-mind.minds.MIND_NAME")' in source, (
            f"{name} logger not genericised to MIND_NAME placeholder"
        )
