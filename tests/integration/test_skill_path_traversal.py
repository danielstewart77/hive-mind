"""Integration tests for path traversal prevention across all skill agents.

Verifies that all three skill functions (planning_genius, code_genius,
code_review_genius) correctly reject malicious paths and accept valid ones,
exercising the full validation pipeline end-to-end.
"""

import os
import tempfile
from unittest.mock import MagicMock, patch

from config import PROJECT_DIR

DOCUMENTS_DIR = PROJECT_DIR / "documents"
VALID_PATH = str(DOCUMENTS_DIR / "integration-test-12345")


class TestAllSkillsRejectDotDotTraversal:
    """Test that all three skills reject ../ traversal paths."""

    def test_all_skills_reject_dot_dot_traversal(self) -> None:
        from agents.skill_planning_genius import planning_genius
        from agents.skill_code_genius import code_genius
        from agents.skill_code_review_genius import code_review_genius

        malicious = "../../.env"
        for skill_fn in [planning_genius, code_genius, code_review_genius]:
            result = skill_fn(malicious)
            assert "Path validation failed" in result, (
                f"{skill_fn.__name__} did not reject traversal path"
            )
            assert "outside the allowed" in result, (
                f"{skill_fn.__name__} error message missing expected text"
            )


class TestAllSkillsRejectAbsoluteEscape:
    """Test that all three skills reject absolute paths outside documents/."""

    def test_all_skills_reject_absolute_escape(self) -> None:
        from agents.skill_planning_genius import planning_genius
        from agents.skill_code_genius import code_genius
        from agents.skill_code_review_genius import code_review_genius

        malicious = "/etc/shadow"
        for skill_fn in [planning_genius, code_genius, code_review_genius]:
            result = skill_fn(malicious)
            assert "Path validation failed" in result, (
                f"{skill_fn.__name__} did not reject absolute escape path"
            )


class TestAllSkillsAcceptValidDocumentsPath:
    """Test that all three skills accept valid paths and reach subprocess."""

    @patch("agents.skill_planning_genius.subprocess.run")
    def test_planning_genius_accepts_valid_path(
        self, mock_run: MagicMock
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        from agents.skill_planning_genius import planning_genius

        result = planning_genius(VALID_PATH)
        mock_run.assert_called_once()
        assert "ok" in result

    @patch("agents.skill_code_genius.subprocess.run")
    def test_code_genius_accepts_valid_path(
        self, mock_run: MagicMock
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        from agents.skill_code_genius import code_genius

        result = code_genius(VALID_PATH)
        mock_run.assert_called_once()
        assert "ok" in result

    @patch("agents.skill_code_review_genius.subprocess.run")
    def test_code_review_genius_accepts_valid_path(
        self, mock_run: MagicMock
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        from agents.skill_code_review_genius import code_review_genius

        result = code_review_genius(VALID_PATH)
        mock_run.assert_called_once()
        assert "ok" in result


class TestSymlinkAttackBlockedAcrossAllSkills:
    """Test that symlink-based escapes are blocked for all skills."""

    def test_symlink_attack_blocked_across_all_skills(self) -> None:
        from agents.skill_planning_genius import planning_genius
        from agents.skill_code_genius import code_genius
        from agents.skill_code_review_genius import code_review_genius

        with tempfile.TemporaryDirectory() as escape_target:
            symlink_path = DOCUMENTS_DIR / "evil_integration_test_symlink"
            try:
                os.symlink(escape_target, str(symlink_path))
                for skill_fn in [planning_genius, code_genius, code_review_genius]:
                    result = skill_fn(str(symlink_path))
                    assert "Path validation failed" in result, (
                        f"{skill_fn.__name__} did not block symlink escape"
                    )
            finally:
                if symlink_path.exists() or symlink_path.is_symlink():
                    os.unlink(str(symlink_path))


class TestValidationModuleImportsCleanly:
    """Test that the validation module can be imported successfully."""

    def test_validation_module_imports_cleanly(self) -> None:
        from core.path_validation import validate_documents_path

        assert callable(validate_documents_path)
