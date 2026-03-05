"""Unit tests for path validation integration in skill agent files.

Tests that planning_genius, code_genius, and code_review_genius all
reject malicious paths gracefully and pass valid paths to subprocess.
"""

from unittest.mock import MagicMock, patch

from config import PROJECT_DIR

DOCUMENTS_DIR = PROJECT_DIR / "documents"
VALID_PATH = str(DOCUMENTS_DIR / "12345")


class TestPlanningGeniusPathValidation:
    """Tests for path validation in planning_genius."""

    def test_planning_genius_rejects_traversal_path(self) -> None:
        from agents.skill_planning_genius import planning_genius

        result = planning_genius("../../../etc/shadow")
        assert "outside the allowed" in result.lower() or "Path validation failed" in result

    def test_planning_genius_rejects_absolute_sensitive_path(self) -> None:
        from agents.skill_planning_genius import planning_genius

        result = planning_genius("/etc/passwd")
        assert "outside the allowed" in result.lower() or "Path validation failed" in result

    @patch("agents.skill_planning_genius.subprocess.run")
    def test_planning_genius_valid_path_calls_subprocess(
        self, mock_run: MagicMock
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="Plan created", stderr="")
        from agents.skill_planning_genius import planning_genius

        result = planning_genius(VALID_PATH)
        mock_run.assert_called_once()
        assert "Plan created" in result


class TestCodeGeniusPathValidation:
    """Tests for path validation in code_genius."""

    def test_code_genius_rejects_traversal_path(self) -> None:
        from agents.skill_code_genius import code_genius

        result = code_genius("../../../etc/shadow")
        assert "outside the allowed" in result.lower() or "Path validation failed" in result

    def test_code_genius_rejects_absolute_sensitive_path(self) -> None:
        from agents.skill_code_genius import code_genius

        result = code_genius("/etc/passwd")
        assert "outside the allowed" in result.lower() or "Path validation failed" in result

    @patch("agents.skill_code_genius.subprocess.run")
    def test_code_genius_valid_path_calls_subprocess(
        self, mock_run: MagicMock
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="Build passed", stderr="")
        from agents.skill_code_genius import code_genius

        result = code_genius(VALID_PATH)
        mock_run.assert_called_once()
        assert "Build passed" in result


class TestCodeReviewGeniusPathValidation:
    """Tests for path validation in code_review_genius."""

    def test_code_review_genius_rejects_traversal_path(self) -> None:
        from agents.skill_code_review_genius import code_review_genius

        result = code_review_genius("../../../etc/shadow")
        assert "outside the allowed" in result.lower() or "Path validation failed" in result

    def test_code_review_genius_rejects_absolute_sensitive_path(self) -> None:
        from agents.skill_code_review_genius import code_review_genius

        result = code_review_genius("/etc/passwd")
        assert "outside the allowed" in result.lower() or "Path validation failed" in result

    @patch("agents.skill_code_review_genius.subprocess.run")
    def test_code_review_genius_valid_path_calls_subprocess(
        self, mock_run: MagicMock
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="Review done", stderr="")
        from agents.skill_code_review_genius import code_review_genius

        result = code_review_genius(VALID_PATH)
        mock_run.assert_called_once()
        assert "Review done" in result
