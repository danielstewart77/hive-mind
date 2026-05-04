"""Unit tests for MindRegistry single-mind mode.

When MIND_ID env var is set, MindRegistry.scan() should only load the target
mind, not all minds in the directory.
"""

import logging
from pathlib import Path

import pytest


def _write_runtime_yaml(mind_dir: Path, name: str, default_model: str = "sonnet",
                        harness: str = "claude_cli",
                        gateway_url: str = "http://hive_mind:8420") -> None:
    """Helper to write a valid runtime.yaml into a directory."""
    import uuid
    mind_dir.mkdir(parents=True, exist_ok=True)
    (mind_dir / "runtime.yaml").write_text(
        f"name: {name}\n"
        f"mind_id: {uuid.uuid4()}\n"
        f"default_model: {default_model}\n"
        f"harness: {harness}\n"
        f"gateway_url: {gateway_url}\n"
    )


class TestSingleMindMode:
    """Tests for MindRegistry single_mind parameter."""

    def test_scan_all_minds_when_no_single_mind(self, tmp_path):
        """Without single_mind, scan() discovers all minds."""
        from core.mind_registry import MindRegistry

        _write_runtime_yaml(tmp_path / "ada", "ada")
        _write_runtime_yaml(tmp_path / "bilby", "bilby")

        registry = MindRegistry(tmp_path)
        registry.scan()

        names = [m.name for m in registry.list_all()]
        assert "ada" in names
        assert "bilby" in names
        assert len(names) == 2

    def test_scan_single_mind_only_loads_target(self, tmp_path):
        """With single_mind='ada', only ada is loaded."""
        from core.mind_registry import MindRegistry

        _write_runtime_yaml(tmp_path / "ada", "ada")
        _write_runtime_yaml(tmp_path / "bilby", "bilby")

        registry = MindRegistry(tmp_path, single_mind="ada")
        registry.scan()

        names = [m.name for m in registry.list_all()]
        assert names == ["ada"]
        assert registry.get("bilby") is None

    def test_scan_single_mind_missing_dir_results_in_empty_registry(self, tmp_path):
        """single_mind='nonexistent' results in empty registry (no entry to register)."""
        from core.mind_registry import MindRegistry

        _write_runtime_yaml(tmp_path / "ada", "ada")

        registry = MindRegistry(tmp_path, single_mind="nonexistent")
        registry.scan()

        assert len(registry.list_all()) == 0
        assert registry.get("nonexistent") is None
