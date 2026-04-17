"""Unit tests for core/mind_registry.py — MIND.md parser and MindRegistry."""

import logging
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Step 1: parse_mind_file tests
# ---------------------------------------------------------------------------

class TestParseMindFile:
    """Tests for parse_mind_file() — YAML frontmatter parser."""

    def test_parse_mind_file_returns_mind_info(self, tmp_path):
        """Valid MIND.md with all required fields returns populated MindInfo."""
        from core.mind_registry import parse_mind_file, MindInfo

        mind_file = tmp_path / "MIND.md"
        mind_file.write_text(
            "---\n"
            "name: ada\n"
            "model: sonnet\n"
            "harness: claude_cli_claude\n"
            "gateway_url: http://hive_mind:8420\n"
            "---\n"
            "I am Ada.\n"
        )

        info = parse_mind_file(mind_file)
        assert isinstance(info, MindInfo)
        assert info.name == "ada"
        assert info.model == "sonnet"
        assert info.harness == "claude_cli_claude"
        assert info.gateway_url == "http://hive_mind:8420"
        assert info.prompt_profile == "default"

    def test_parse_mind_file_extracts_prompt_profile(self, tmp_path):
        """Optional prompt_profile field is parsed into MindInfo."""
        from core.mind_registry import parse_mind_file

        mind_file = tmp_path / "MIND.md"
        mind_file.write_text(
            "---\n"
            "name: nagatha\n"
            "model: codex\n"
            "harness: codex_cli_codex\n"
            "gateway_url: http://nagatha:8420\n"
            "prompt_profile: programmer\n"
            "---\n"
            "I am Nagatha.\n"
        )

        info = parse_mind_file(mind_file)
        assert info.prompt_profile == "programmer"

    def test_parse_mind_file_extracts_soul_seed(self, tmp_path):
        """Markdown body after closing --- is returned as soul_seed."""
        from core.mind_registry import parse_mind_file

        mind_file = tmp_path / "MIND.md"
        mind_file.write_text(
            "---\n"
            "name: ada\n"
            "model: sonnet\n"
            "harness: claude_cli_claude\n"
            "gateway_url: http://hive_mind:8420\n"
            "---\n"
            "I am Ada -- a voice of the Hivemind.\n"
        )

        info = parse_mind_file(mind_file)
        assert info.soul_seed.strip() == "I am Ada -- a voice of the Hivemind."

    def test_parse_mind_file_missing_required_field_raises(self, tmp_path):
        """MIND.md missing 'name' field raises ValueError."""
        from core.mind_registry import parse_mind_file

        mind_file = tmp_path / "MIND.md"
        mind_file.write_text(
            "---\n"
            "model: sonnet\n"
            "harness: claude_cli_claude\n"
            "gateway_url: http://hive_mind:8420\n"
            "---\n"
        )

        with pytest.raises(ValueError, match="name"):
            parse_mind_file(mind_file)

    def test_parse_mind_file_missing_harness_raises(self, tmp_path):
        """MIND.md missing 'harness' field raises ValueError."""
        from core.mind_registry import parse_mind_file

        mind_file = tmp_path / "MIND.md"
        mind_file.write_text(
            "---\n"
            "name: ada\n"
            "model: sonnet\n"
            "gateway_url: http://hive_mind:8420\n"
            "---\n"
        )

        with pytest.raises(ValueError, match="harness"):
            parse_mind_file(mind_file)

    def test_parse_mind_file_optional_remote_defaults_false(self, tmp_path):
        """Omitting 'remote' field defaults to False."""
        from core.mind_registry import parse_mind_file

        mind_file = tmp_path / "MIND.md"
        mind_file.write_text(
            "---\n"
            "name: ada\n"
            "model: sonnet\n"
            "harness: claude_cli_claude\n"
            "gateway_url: http://hive_mind:8420\n"
            "---\n"
        )

        info = parse_mind_file(mind_file)
        assert info.remote is False

    def test_parse_mind_file_remote_true(self, tmp_path):
        """Include 'remote: true' sets MindInfo.remote to True."""
        from core.mind_registry import parse_mind_file

        mind_file = tmp_path / "MIND.md"
        mind_file.write_text(
            "---\n"
            "name: ada\n"
            "model: sonnet\n"
            "harness: claude_cli_claude\n"
            "gateway_url: http://hive_mind:8420\n"
            "remote: true\n"
            "---\n"
        )

        info = parse_mind_file(mind_file)
        assert info.remote is True

    def test_parse_mind_file_no_frontmatter_raises(self, tmp_path):
        """File with no --- delimiters raises ValueError."""
        from core.mind_registry import parse_mind_file

        mind_file = tmp_path / "MIND.md"
        mind_file.write_text("Just some markdown without frontmatter.\n")

        with pytest.raises(ValueError):
            parse_mind_file(mind_file)

    def test_parse_mind_file_empty_body_ok(self, tmp_path):
        """Frontmatter only, blank after second ---, soul_seed is empty string."""
        from core.mind_registry import parse_mind_file

        mind_file = tmp_path / "MIND.md"
        mind_file.write_text(
            "---\n"
            "name: ada\n"
            "model: sonnet\n"
            "harness: claude_cli_claude\n"
            "gateway_url: http://hive_mind:8420\n"
            "---\n"
        )

        info = parse_mind_file(mind_file)
        assert info.soul_seed == ""


# ---------------------------------------------------------------------------
# Step 2: MindRegistry tests
# ---------------------------------------------------------------------------

def _write_mind_md(mind_dir: Path, name: str, model: str = "sonnet",
                   harness: str = "claude_cli_claude",
                   gateway_url: str = "http://hive_mind:8420",
                   prompt_profile: str = "default",
                   body: str = "") -> None:
    """Helper to write a valid MIND.md into a directory."""
    mind_dir.mkdir(parents=True, exist_ok=True)
    (mind_dir / "MIND.md").write_text(
        f"---\n"
        f"name: {name}\n"
        f"model: {model}\n"
        f"harness: {harness}\n"
        f"gateway_url: {gateway_url}\n"
        f"prompt_profile: {prompt_profile}\n"
        f"---\n"
        f"{body}\n"
    )


class TestMindRegistry:
    """Tests for MindRegistry class -- filesystem scan and lookup."""

    def test_registry_scan_discovers_minds(self, tmp_path):
        """scan() discovers all subdirs containing MIND.md."""
        from core.mind_registry import MindRegistry

        _write_mind_md(tmp_path / "ada", "ada")
        _write_mind_md(tmp_path / "bob", "bob", model="gpt-oss:20b-32k",
                       harness="claude_cli_ollama")

        registry = MindRegistry(tmp_path)
        registry.scan()

        names = [m.name for m in registry.list_all()]
        assert "ada" in names
        assert "bob" in names

    def test_registry_get_returns_mind_info(self, tmp_path):
        """get() returns MindInfo with correct fields."""
        from core.mind_registry import MindRegistry

        _write_mind_md(tmp_path / "ada", "ada", model="sonnet",
                       harness="claude_cli_claude",
                       gateway_url="http://hive_mind:8420")

        registry = MindRegistry(tmp_path)
        registry.scan()

        info = registry.get("ada")
        assert info is not None
        assert info.name == "ada"
        assert info.model == "sonnet"
        assert info.harness == "claude_cli_claude"
        assert info.gateway_url == "http://hive_mind:8420"
        assert info.prompt_profile == "default"

    def test_registry_get_returns_prompt_profile(self, tmp_path):
        """get() returns prompt profile metadata for prompt selection."""
        from core.mind_registry import MindRegistry

        _write_mind_md(
            tmp_path / "nagatha",
            "nagatha",
            model="codex",
            harness="codex_cli_codex",
            gateway_url="http://nagatha:8420",
            prompt_profile="programmer",
        )

        registry = MindRegistry(tmp_path)
        registry.scan()

        info = registry.get("nagatha")
        assert info is not None
        assert info.prompt_profile == "programmer"

    def test_registry_get_unknown_returns_none(self, tmp_path):
        """get() for nonexistent mind returns None."""
        from core.mind_registry import MindRegistry

        registry = MindRegistry(tmp_path)
        registry.scan()

        assert registry.get("nonexistent") is None

    def test_registry_list_all_returns_all_minds(self, tmp_path):
        """list_all() returns correct number of minds."""
        from core.mind_registry import MindRegistry

        _write_mind_md(tmp_path / "ada", "ada")
        _write_mind_md(tmp_path / "bob", "bob")
        _write_mind_md(tmp_path / "nagatha", "nagatha")

        registry = MindRegistry(tmp_path)
        registry.scan()

        assert len(registry.list_all()) == 3

    def test_registry_scan_skips_dirs_without_mind_md(self, tmp_path):
        """Subdirs without MIND.md are not registered."""
        from core.mind_registry import MindRegistry

        _write_mind_md(tmp_path / "ada", "ada")
        # Create a dir with only implementation.py, no MIND.md
        other_dir = tmp_path / "orphan"
        other_dir.mkdir()
        (other_dir / "implementation.py").write_text("# no MIND.md here\n")

        registry = MindRegistry(tmp_path)
        registry.scan()

        assert registry.get("orphan") is None
        assert len(registry.list_all()) == 1

    def test_registry_scan_logs_registered_minds(self, tmp_path, caplog):
        """scan() logs a message for each discovered mind."""
        from core.mind_registry import MindRegistry

        _write_mind_md(tmp_path / "ada", "ada")
        _write_mind_md(tmp_path / "bob", "bob")

        registry = MindRegistry(tmp_path)
        with caplog.at_level(logging.INFO):
            registry.scan()

        assert any("Registered mind:" in r.message and "ada" in r.message for r in caplog.records)
        assert any("Registered mind:" in r.message and "bob" in r.message for r in caplog.records)
