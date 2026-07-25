"""Structural guards for the shared harness modules.

The deployed minds run ``minds.harness.claude_cli`` / ``minds.harness.codex_cli``
directly (selected by ``MIND_NAME``), so the shipped harness is the production
wiring by construction. These tests pin the contract that keeps it shippable:
both modules import cleanly on a fresh clone (falling back to the tracked
``minds/example`` config when ``MIND_NAME`` is unset), and no deployment's
mind names are baked into the harness source.
"""

from __future__ import annotations

import ast
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parents[2] / "minds" / "harness"


def test_claude_cli_imports_with_example_fallback() -> None:
    from minds.harness import claude_cli

    assert claude_cli.NAME == claude_cli.RUNTIME["name"]
    assert claude_cli.DEFAULT_MODEL
    assert claude_cli.app.title == f"Mind: {claude_cli.NAME}"


def test_codex_cli_imports_with_example_fallback() -> None:
    from minds.harness import codex_cli

    assert codex_cli.NAME == codex_cli.RUNTIME["name"]
    assert codex_cli.DEFAULT_MODEL
    assert codex_cli.app.title == f"Mind: {codex_cli.NAME}"


def test_harness_sources_parse_and_bake_in_no_mind_names() -> None:
    """A harness serves any mind; a literal mind name means a leak from a
    deployment's roster crept back into shared code. ``example`` is the one
    permitted name — it's the tracked fallback config."""
    deployment_names = {"ada", "bob", "bilby", "nagatha", "hex", "arnold", "skippy", "mordecai"}
    for path in HARNESS_DIR.glob("*.py"):
        source = path.read_text()
        ast.parse(source)  # unparseable harness = unbootable mind
        lowered = source.lower()
        for name in deployment_names:
            assert f'"{name}"' not in lowered and f"'{name}'" not in lowered, (
                f"{path.name} bakes in mind name {name!r}"
            )


def test_example_runtime_config_is_complete() -> None:
    import yaml

    runtime = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "minds" / "example" / "runtime.yaml").read_text()
    )
    for key in ("name", "mind_id", "harness", "provider", "default_model"):
        assert key in runtime, f"example runtime.yaml missing {key}"
    assert runtime["name"] == "example"
