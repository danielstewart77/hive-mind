"""Unit tests for core/mind_registry.py — runtime.yaml parser and MindRegistry."""

import logging
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Step 1: parse_mind_file tests (runtime.yaml only — MIND.md parsing removed)
# ---------------------------------------------------------------------------

class TestParseMindFile:
    """Tests for parse_mind_file() — runtime.yaml parser."""

    def test_parse_runtime_yaml_returns_mind_info(self, tmp_path):
        """Valid runtime.yaml with all required fields returns populated MindInfo."""
        from core.mind_registry import parse_mind_file, MindInfo

        runtime_file = tmp_path / "runtime.yaml"
        runtime_file.write_text(
            "name: ada\n"
            "mind_id: 00000000-0000-0000-0000-000000000001\n"
            "default_model: sonnet\n"
            "harness: claude_cli\n"
            "gateway_url: http://hive_mind:8420\n"
        )

        info = parse_mind_file(runtime_file)
        assert isinstance(info, MindInfo)
        assert info.name == "ada"
        assert info.model == "sonnet"
        assert info.harness == "claude_cli"
        assert info.gateway_url == "http://hive_mind:8420"
        assert info.prompt_files == []

    def test_parse_runtime_yaml_extracts_prompt_files(self, tmp_path):
        """Optional prompt_files field is parsed into MindInfo."""
        from core.mind_registry import parse_mind_file

        runtime_file = tmp_path / "runtime.yaml"
        runtime_file.write_text(
            "name: nagatha\n"
            "mind_id: 00000000-0000-0000-0000-000000000002\n"
            "default_model: codex\n"
            "harness: codex_cli\n"
            "gateway_url: http://nagatha:8420\n"
            "prompt_files:\n"
            "  - prompts/common.md\n"
            "  - prompts/profile.md\n"
        )

        info = parse_mind_file(runtime_file)
        assert info.prompt_files == ["prompts/common.md", "prompts/profile.md"]

    def test_parse_runtime_yaml_soul_seed_is_always_empty(self, tmp_path):
        """soul_seed is always empty string — runtime.yaml has no body."""
        from core.mind_registry import parse_mind_file

        runtime_file = tmp_path / "runtime.yaml"
        runtime_file.write_text(
            "name: ada\n"
            "mind_id: 00000000-0000-0000-0000-000000000003\n"
            "default_model: sonnet\n"
            "harness: claude_cli\n"
            "gateway_url: http://hive_mind:8420\n"
        )

        info = parse_mind_file(runtime_file)
        assert info.soul_seed == ""

    def test_parse_runtime_yaml_missing_required_field_raises(self, tmp_path):
        """runtime.yaml missing 'name' field raises ValueError."""
        from core.mind_registry import parse_mind_file

        runtime_file = tmp_path / "runtime.yaml"
        runtime_file.write_text(
            "default_model: sonnet\n"
            "harness: claude_cli\n"
            "gateway_url: http://hive_mind:8420\n"
        )

        with pytest.raises(ValueError, match="name"):
            parse_mind_file(runtime_file)

    def test_parse_runtime_yaml_missing_harness_raises(self, tmp_path):
        """runtime.yaml missing 'harness' field raises ValueError."""
        from core.mind_registry import parse_mind_file

        runtime_file = tmp_path / "runtime.yaml"
        runtime_file.write_text(
            "name: ada\n"
            "mind_id: 00000000-0000-0000-0000-000000000004\n"
            "default_model: sonnet\n"
            "gateway_url: http://hive_mind:8420\n"
        )

        with pytest.raises(ValueError, match="harness"):
            parse_mind_file(runtime_file)

    def test_parse_runtime_yaml_missing_default_model_raises(self, tmp_path):
        """runtime.yaml missing 'default_model' field raises ValueError."""
        from core.mind_registry import parse_mind_file

        runtime_file = tmp_path / "runtime.yaml"
        runtime_file.write_text(
            "name: ada\n"
            "mind_id: 00000000-0000-0000-0000-000000000005\n"
            "harness: claude_cli\n"
            "gateway_url: http://hive_mind:8420\n"
        )

        with pytest.raises(ValueError, match="default_model"):
            parse_mind_file(runtime_file)

    def test_parse_runtime_yaml_optional_remote_defaults_false(self, tmp_path):
        """Omitting 'remote' field defaults to False."""
        from core.mind_registry import parse_mind_file

        runtime_file = tmp_path / "runtime.yaml"
        runtime_file.write_text(
            "name: ada\n"
            "mind_id: 00000000-0000-0000-0000-000000000006\n"
            "default_model: sonnet\n"
            "harness: claude_cli\n"
            "gateway_url: http://hive_mind:8420\n"
        )

        info = parse_mind_file(runtime_file)
        assert info.remote is False

    def test_parse_runtime_yaml_remote_true(self, tmp_path):
        """Include 'remote: true' sets MindInfo.remote to True."""
        from core.mind_registry import parse_mind_file

        runtime_file = tmp_path / "runtime.yaml"
        runtime_file.write_text(
            "name: ada\n"
            "mind_id: 00000000-0000-0000-0000-000000000007\n"
            "default_model: sonnet\n"
            "harness: claude_cli\n"
            "gateway_url: http://hive_mind:8420\n"
            "remote: true\n"
        )

        info = parse_mind_file(runtime_file)
        assert info.remote is True

    def test_parse_mind_file_rejects_non_runtime_yaml_path(self, tmp_path):
        """parse_mind_file refuses any file that isn't named runtime.yaml."""
        from core.mind_registry import parse_mind_file

        mind_file = tmp_path / "MIND.md"
        mind_file.write_text(
            "---\nname: ada\nmodel: sonnet\nharness: claude_cli\n"
            "gateway_url: http://hive_mind:8420\n---\nI am Ada.\n"
        )

        with pytest.raises(ValueError, match="runtime.yaml"):
            parse_mind_file(mind_file)


# ---------------------------------------------------------------------------
# Step 2: MindRegistry tests
# ---------------------------------------------------------------------------

def _write_runtime_yaml(mind_dir: Path, name: str, default_model: str = "sonnet",
                        harness: str = "claude_cli",
                        gateway_url: str = "http://hive_mind:8420",
                        prompt_files: list[str] | None = None,
                        mind_id: str | None = None) -> None:
    """Helper to write a valid runtime.yaml into a directory."""
    import uuid
    mind_dir.mkdir(parents=True, exist_ok=True)
    prompt_files = prompt_files or []
    prompt_lines = "".join(f"  - {path}\n" for path in prompt_files)
    mid = mind_id or str(uuid.uuid4())
    (mind_dir / "runtime.yaml").write_text(
        f"name: {name}\n"
        f"mind_id: {mid}\n"
        f"default_model: {default_model}\n"
        f"harness: {harness}\n"
        f"gateway_url: {gateway_url}\n"
        f"prompt_files:\n"
        f"{prompt_lines}"
    )


class TestMindRegistry:
    """Tests for MindRegistry class -- filesystem scan and lookup."""

    def test_registry_scan_discovers_minds(self, tmp_path):
        """scan() discovers all subdirs containing runtime.yaml."""
        from core.mind_registry import MindRegistry

        _write_runtime_yaml(tmp_path / "ada", "ada")
        _write_runtime_yaml(tmp_path / "bob", "bob", default_model="gpt-oss:20b-32k",
                            harness="claude_cli")

        registry = MindRegistry(tmp_path)
        registry.scan()

        names = [m.name for m in registry.list_all()]
        assert "ada" in names
        assert "bob" in names

    def test_registry_get_returns_mind_info(self, tmp_path):
        """get() returns MindInfo with correct fields."""
        from core.mind_registry import MindRegistry

        _write_runtime_yaml(tmp_path / "ada", "ada", default_model="sonnet",
                            harness="claude_cli",
                            gateway_url="http://hive_mind:8420")

        registry = MindRegistry(tmp_path)
        registry.scan()

        info = registry.get("ada")
        assert info is not None
        assert info.name == "ada"
        assert info.model == "sonnet"
        assert info.harness == "claude_cli"
        assert info.gateway_url == "http://hive_mind:8420"
        assert info.prompt_files == []

    def test_registry_get_returns_prompt_files(self, tmp_path):
        """get() returns prompt file metadata for prompt selection."""
        from core.mind_registry import MindRegistry

        _write_runtime_yaml(
            tmp_path / "nagatha",
            "nagatha",
            default_model="codex",
            harness="codex_cli",
            gateway_url="http://nagatha:8420",
            prompt_files=["prompts/common.md", "prompts/profile.md"],
        )

        registry = MindRegistry(tmp_path)
        registry.scan()

        info = registry.get("nagatha")
        assert info is not None
        assert info.prompt_files == ["prompts/common.md", "prompts/profile.md"]

    def test_registry_get_unknown_returns_none(self, tmp_path):
        """get() for nonexistent mind returns None."""
        from core.mind_registry import MindRegistry

        registry = MindRegistry(tmp_path)
        registry.scan()

        assert registry.get("nonexistent") is None

    def test_registry_list_all_returns_all_minds(self, tmp_path):
        """list_all() returns correct number of minds."""
        from core.mind_registry import MindRegistry

        _write_runtime_yaml(tmp_path / "ada", "ada")
        _write_runtime_yaml(tmp_path / "bob", "bob")
        _write_runtime_yaml(tmp_path / "nagatha", "nagatha")

        registry = MindRegistry(tmp_path)
        registry.scan()

        assert len(registry.list_all()) == 3

    def test_registry_scan_skips_dirs_without_runtime_yaml(self, tmp_path, caplog):
        """Subdirs without runtime.yaml are not registered and an error is logged."""
        from core.mind_registry import MindRegistry

        _write_runtime_yaml(tmp_path / "ada", "ada")
        # Create a dir with only implementation.py, no runtime.yaml
        other_dir = tmp_path / "orphan"
        other_dir.mkdir()
        (other_dir / "implementation.py").write_text("# no runtime.yaml here\n")

        registry = MindRegistry(tmp_path)
        with caplog.at_level(logging.ERROR):
            registry.scan()

        assert registry.get("orphan") is None
        assert len(registry.list_all()) == 1
        assert any("orphan" in r.message and "missing runtime.yaml" in r.message
                   for r in caplog.records)

    def test_registry_scan_logs_registered_minds(self, tmp_path, caplog):
        """scan() logs a message for each discovered mind."""
        from core.mind_registry import MindRegistry

        _write_runtime_yaml(tmp_path / "ada", "ada")
        _write_runtime_yaml(tmp_path / "bob", "bob")

        registry = MindRegistry(tmp_path)
        with caplog.at_level(logging.INFO):
            registry.scan()

        assert any("Registered mind:" in r.message and "ada" in r.message for r in caplog.records)
        assert any("Registered mind:" in r.message and "bob" in r.message for r in caplog.records)
