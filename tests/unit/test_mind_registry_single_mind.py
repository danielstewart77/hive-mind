"""Unit tests for MindRegistry single-mind mode.

When MIND_ID env var is set, MindRegistry.scan() should only load the target
mind, not all minds in the directory.
"""

import logging
from pathlib import Path

import pytest


def _write_mind_md(mind_dir: Path, name: str, model: str = "sonnet",
                   harness: str = "claude_cli_claude",
                   gateway_url: str = "http://hive_mind:8420") -> None:
    """Helper to write a valid MIND.md into a directory."""
    mind_dir.mkdir(parents=True, exist_ok=True)
    (mind_dir / "MIND.md").write_text(
        f"---\n"
        f"name: {name}\n"
        f"model: {model}\n"
        f"harness: {harness}\n"
        f"gateway_url: {gateway_url}\n"
        f"---\n"
        f"I am {name}.\n"
    )


class TestSingleMindMode:
    """Tests for MindRegistry single_mind parameter."""

    def test_scan_all_minds_when_no_single_mind(self, tmp_path):
        """Without single_mind, scan() discovers all minds."""
        from core.mind_registry import MindRegistry

        _write_mind_md(tmp_path / "ada", "ada")
        _write_mind_md(tmp_path / "bilby", "bilby")

        registry = MindRegistry(tmp_path)
        registry.scan()

        names = [m.name for m in registry.list_all()]
        assert "ada" in names
        assert "bilby" in names
        assert len(names) == 2

    def test_scan_single_mind_only_loads_target(self, tmp_path):
        """With single_mind='ada', only ada is loaded."""
        from core.mind_registry import MindRegistry

        _write_mind_md(tmp_path / "ada", "ada")
        _write_mind_md(tmp_path / "bilby", "bilby")

        registry = MindRegistry(tmp_path, single_mind="ada")
        registry.scan()

        names = [m.name for m in registry.list_all()]
        assert names == ["ada"]
        assert registry.get("bilby") is None

    def test_scan_single_mind_missing_dir_logs_warning(self, tmp_path, caplog):
        """single_mind='nonexistent' results in empty registry and a warning."""
        from core.mind_registry import MindRegistry

        _write_mind_md(tmp_path / "ada", "ada")

        registry = MindRegistry(tmp_path, single_mind="nonexistent")
        with caplog.at_level(logging.WARNING):
            registry.scan()

        assert len(registry.list_all()) == 0
        assert any("nonexistent" in r.message for r in caplog.records)
