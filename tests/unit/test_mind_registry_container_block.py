"""Unit tests for ContainerConfig parsing in core/mind_registry.py.

Tests the optional container block in MIND.md frontmatter.
"""

from pathlib import Path

import pytest


def _write_mind_md(path: Path, frontmatter_extra: str = "", body: str = "") -> Path:
    """Write a MIND.md with optional extra frontmatter fields."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "---\n"
        "name: ada\n"
        "model: sonnet\n"
        "harness: claude_cli_claude\n"
        "gateway_url: http://hive_mind:8420\n"
        f"{frontmatter_extra}"
        "---\n"
        f"{body}\n"
    )
    path.write_text(content)
    return path


class TestContainerBlock:
    """Tests for container block parsing in parse_mind_file()."""

    def test_parse_mind_file_without_container_block(self, tmp_path):
        """MIND.md without container key has container=None."""
        from core.mind_registry import parse_mind_file

        mind_file = _write_mind_md(tmp_path / "MIND.md")
        info = parse_mind_file(mind_file)
        assert info.container is None

    def test_parse_mind_file_with_container_block(self, tmp_path):
        """MIND.md with container block populates ContainerConfig."""
        from core.mind_registry import parse_mind_file, ContainerConfig

        extra = (
            "container:\n"
            "  image: custom:latest\n"
            "  volumes:\n"
            "    - /data:/data:ro\n"
            "  environment:\n"
            "    - DEBUG=1\n"
            "  networks:\n"
            "    - special\n"
        )
        mind_file = _write_mind_md(tmp_path / "MIND.md", frontmatter_extra=extra)
        info = parse_mind_file(mind_file)

        assert info.container is not None
        assert isinstance(info.container, ContainerConfig)
        assert info.container.image == "custom:latest"
        assert info.container.volumes == ["/data:/data:ro"]
        assert info.container.environment == ["DEBUG=1"]
        assert info.container.networks == ["special"]

    def test_parse_mind_file_container_defaults(self, tmp_path):
        """MIND.md with empty container block uses defaults."""
        from core.mind_registry import parse_mind_file

        extra = "container: {}\n"
        mind_file = _write_mind_md(tmp_path / "MIND.md", frontmatter_extra=extra)
        info = parse_mind_file(mind_file)

        assert info.container is not None
        assert info.container.image == "hive_mind:latest"
        assert info.container.volumes == []
        assert info.container.environment == []
        assert info.container.networks == []

    def test_parse_mind_file_container_preserves_existing_fields(self, tmp_path):
        """Existing fields (name, model, harness, gateway_url) still parsed correctly."""
        from core.mind_registry import parse_mind_file

        extra = "container: {}\n"
        mind_file = _write_mind_md(
            tmp_path / "MIND.md",
            frontmatter_extra=extra,
            body="I am Ada.",
        )
        info = parse_mind_file(mind_file)

        assert info.name == "ada"
        assert info.model == "sonnet"
        assert info.harness == "claude_cli_claude"
        assert info.gateway_url == "http://hive_mind:8420"
        assert info.soul_seed == "I am Ada."
        assert info.container is not None
