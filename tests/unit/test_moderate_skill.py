"""Tests for the /moderate skill file."""

from pathlib import Path


class TestModerateSkill:
    """Verify /moderate skill files exist with correct structure."""

    def test_moderate_skill_file_exists(self):
        path = Path(__file__).resolve().parents[2] / ".claude" / "skills" / "moderate" / "SKILL.md"
        assert path.exists(), f"Expected {path} to exist"

    def test_moderate_skill_has_yaml_frontmatter(self):
        path = Path(__file__).resolve().parents[2] / ".claude" / "skills" / "moderate" / "SKILL.md"
        content = path.read_text()
        assert content.startswith("---")
        assert "name: moderate" in content

    def test_moderate_skill_is_user_invocable(self):
        path = Path(__file__).resolve().parents[2] / ".claude" / "skills" / "moderate" / "SKILL.md"
        content = path.read_text()
        assert "user-invocable: true" in content

    def test_moderate_skill_references_forward_to_mind(self):
        path = Path(__file__).resolve().parents[2] / ".claude" / "skills" / "moderate" / "SKILL.md"
        content = path.read_text()
        assert "forward_to_mind" in content

    def test_moderate_spec_file_exists(self):
        path = Path(__file__).resolve().parents[2] / "specs" / "skills" / "moderate" / "SKILL.md"
        assert path.exists(), f"Expected {path} to exist"
