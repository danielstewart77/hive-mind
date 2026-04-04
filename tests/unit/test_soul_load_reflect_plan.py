"""Tests for the soul-load-reflect plan file.

Verifies the plan file exists and Phase 1 is marked as complete.
"""

from pathlib import Path

PLAN_PATH = Path(__file__).resolve().parents[2] / "plans" / "soul-load-reflect.md"


class TestSoulLoadReflectPlan:
    """Verify the plan file exists and tracks Phase 1 completion."""

    def test_plan_file_exists(self) -> None:
        assert PLAN_PATH.exists(), f"Plan file not found at {PLAN_PATH}"

    def test_plan_phase1_marked_complete(self) -> None:
        """Phase 1 items should be marked as complete in the Status section."""
        content = PLAN_PATH.read_text()
        # The plan should have an explicit Status section with Phase 1 complete
        assert "## Status" in content, "Plan should have a Status section"
        assert "Phase 1: Complete" in content, "Phase 1 should be marked as Complete"
        # Both implementation items should be checked off
        assert "[x] Modify `soul_nudge.sh`" in content, (
            "soul_nudge.sh modification should be checked off"
        )
        assert "[x] Update `self-reflect` SKILL.md" in content, (
            "SKILL.md update should be checked off"
        )
