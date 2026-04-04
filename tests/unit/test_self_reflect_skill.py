"""Tests for the /self-reflect skill file.

Verifies the SKILL.md exists with correct structure and contains the
deduplication check that prevents writing duplicate graph nodes for patterns
already present in loaded context.
"""

import re
from pathlib import Path

SKILL_PATH = Path(__file__).resolve().parents[2] / ".claude" / "skills" / "self-reflect" / "SKILL.md"


class TestSelfReflectSkillExists:
    """Basic file existence and structure checks."""

    def test_self_reflect_skill_exists(self) -> None:
        assert SKILL_PATH.exists(), f"Expected {SKILL_PATH} to exist"

    def test_self_reflect_skill_has_yaml_frontmatter(self) -> None:
        content = SKILL_PATH.read_text()
        assert content.startswith("---"), "SKILL.md should start with YAML frontmatter"
        assert "name: self-reflect" in content, "Frontmatter should contain name: self-reflect"


class TestSelfReflectSkillModes:
    """Verify both --load and --reflect modes are documented."""

    def test_self_reflect_skill_has_load_mode(self) -> None:
        content = SKILL_PATH.read_text()
        assert "--load" in content, "Skill should describe --load mode"

    def test_self_reflect_skill_has_reflect_mode(self) -> None:
        content = SKILL_PATH.read_text()
        assert "--reflect" in content, "Skill should describe --reflect mode"


class TestSelfReflectSkillDeduplication:
    """Verify Step 6 contains loaded-context deduplication check."""

    def test_self_reflect_skill_step6_checks_loaded_context(self) -> None:
        """Step 6 should instruct checking loaded context before writing graph nodes."""
        content = SKILL_PATH.read_text()
        # Find the Step 6 section
        step6_match = re.search(r"### Step 6.*?(?=### Step 7|$)", content, re.DOTALL)
        assert step6_match is not None, "Step 6 section not found"
        step6_text = step6_match.group(0).lower()
        assert any(
            phrase in step6_text
            for phrase in ["loaded context", "already present", "already captured", "already exists"]
        ), "Step 6 should reference checking loaded context before writing"

    def test_self_reflect_skill_mentions_reinforced(self) -> None:
        """Existing patterns should be marked as 'reinforced' rather than duplicated."""
        content = SKILL_PATH.read_text()
        assert "reinforced" in content.lower(), (
            "Skill should mention marking existing patterns as 'reinforced'"
        )

    def test_self_reflect_skill_no_duplicate_writes(self) -> None:
        """Skill should contain language about skipping writes for already-captured patterns."""
        content = SKILL_PATH.read_text()
        lower_content = content.lower()
        assert any(
            phrase in lower_content
            for phrase in ["skip", "already captured", "already present", "do not call"]
        ), "Skill should instruct skipping writes for already-captured patterns"

    def test_self_reflect_skill_step6_before_step7(self) -> None:
        """The deduplication check (Step 6) must come before write instructions (Step 7)."""
        content = SKILL_PATH.read_text()
        step6_pos = content.find("### Step 6")
        step7_pos = content.find("### Step 7")
        assert step6_pos != -1, "Step 6 section not found"
        assert step7_pos != -1, "Step 7 section not found"
        assert step6_pos < step7_pos, "Step 6 must come before Step 7"
