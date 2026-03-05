"""Unit tests for the techconfig verifier module (core.techconfig_verifier)."""

from unittest.mock import patch, MagicMock
import subprocess

import pytest


class TestVerifyEntryWithCodebaseRef:
    """Tests for verify_entry when codebase_ref is provided."""

    def test_verify_entry_file_exists_and_symbol_found_returns_verified(self) -> None:
        """Entry with codebase_ref='server.py' and content mentioning 'FastAPI' returns verified."""
        from core.techconfig_verifier import verify_entry

        with (
            patch("core.techconfig_verifier._check_file_exists", return_value=True),
            patch("core.techconfig_verifier._check_symbol_in_file", return_value=True),
        ):
            result = verify_entry(
                content="server.py uses FastAPI as the gateway framework",
                element_id="elem-1",
                codebase_ref="server.py",
            )
        assert result.status == "verified"
        assert result.element_id == "elem-1"
        assert result.codebase_ref == "server.py"

    def test_verify_entry_file_exists_but_symbol_not_found_returns_pruned(self) -> None:
        """Entry with codebase_ref='server.py' and content mentioning a non-existent function returns pruned."""
        from core.techconfig_verifier import verify_entry

        with (
            patch("core.techconfig_verifier._check_file_exists", return_value=True),
            patch("core.techconfig_verifier._check_symbol_in_file", return_value=False),
        ):
            result = verify_entry(
                content="server.py uses the nonexistent_function() for routing",
                element_id="elem-2",
                codebase_ref="server.py",
            )
        assert result.status == "pruned"
        assert "not found" in result.reason.lower() or "symbol" in result.reason.lower()

    def test_verify_entry_file_does_not_exist_returns_flagged(self) -> None:
        """Entry with codebase_ref='nonexistent.py' returns flagged for review."""
        from core.techconfig_verifier import verify_entry

        with patch("core.techconfig_verifier._check_file_exists", return_value=False):
            result = verify_entry(
                content="nonexistent.py handles X",
                element_id="elem-3",
                codebase_ref="nonexistent.py",
            )
        assert result.status == "flagged"
        assert "not exist" in result.reason.lower() or "missing" in result.reason.lower()


class TestVerifyEntryWithoutCodebaseRef:
    """Tests for verify_entry when codebase_ref is absent."""

    def test_verify_entry_no_codebase_ref_infers_file_and_verifies(self) -> None:
        """Entry without codebase_ref but with content like 'server.py handles /sessions' infers the file."""
        from core.techconfig_verifier import verify_entry

        with (
            patch("core.techconfig_verifier._extract_file_references", return_value=["server.py"]),
            patch("core.techconfig_verifier._check_file_exists", return_value=True),
            patch("core.techconfig_verifier._check_symbol_in_file", return_value=True),
        ):
            result = verify_entry(
                content="server.py handles /sessions endpoint",
                element_id="elem-4",
                codebase_ref=None,
            )
        assert result.status == "verified"

    def test_verify_entry_no_codebase_ref_no_file_match_returns_flagged(self) -> None:
        """Entry without codebase_ref and no file inference possible returns flagged."""
        from core.techconfig_verifier import verify_entry

        with (
            patch("core.techconfig_verifier._extract_file_references", return_value=[]),
            patch("core.techconfig_verifier._extract_keywords", return_value=["something"]),
            patch("core.techconfig_verifier._check_symbol_in_project", return_value=False),
        ):
            result = verify_entry(
                content="Some vague technical configuration note",
                element_id="elem-5",
                codebase_ref=None,
            )
        assert result.status == "flagged"

    def test_verify_entry_no_codebase_ref_file_inferred_but_symbol_missing_returns_pruned(self) -> None:
        """Entry without codebase_ref, file inferred from content, but symbol not found returns pruned."""
        from core.techconfig_verifier import verify_entry

        with (
            patch("core.techconfig_verifier._extract_file_references", return_value=["core/sessions.py"]),
            patch("core.techconfig_verifier._check_file_exists", return_value=True),
            patch("core.techconfig_verifier._check_symbol_in_file", return_value=False),
        ):
            result = verify_entry(
                content="core/sessions.py uses the old_function() for reaping",
                element_id="elem-6",
                codebase_ref=None,
            )
        assert result.status == "pruned"


class TestVerifyEntryEdgeCases:
    """Edge cases for verify_entry."""

    def test_verify_entry_empty_content_returns_flagged(self) -> None:
        """Empty content string returns flagged."""
        from core.techconfig_verifier import verify_entry

        result = verify_entry(
            content="",
            element_id="elem-7",
            codebase_ref=None,
        )
        assert result.status == "flagged"


class TestExtractKeywords:
    """Tests for _extract_keywords helper."""

    def test_extract_keywords_from_content(self) -> None:
        """Extracts filenames, function names, and config keys from content text."""
        from core.techconfig_verifier import _extract_keywords

        content = "The send_message function in sessions.py uses model_registry"
        keywords = _extract_keywords(content)
        assert isinstance(keywords, list)
        assert len(keywords) > 0
        # Should include function-like names
        assert "send_message" in keywords or any("send_message" in kw for kw in keywords)


class TestExtractFileReferences:
    """Tests for _extract_file_references helper."""

    def test_extract_file_references_finds_python_files(self) -> None:
        """Finds .py file references in content."""
        from core.techconfig_verifier import _extract_file_references

        content = "server.py uses FastAPI and core/sessions.py manages processes"
        refs = _extract_file_references(content)
        assert "server.py" in refs
        assert "core/sessions.py" in refs

    def test_extract_file_references_finds_yaml_files(self) -> None:
        """Finds .yaml file references in content."""
        from core.techconfig_verifier import _extract_file_references

        content = "Non-secret settings are stored in config.yaml"
        refs = _extract_file_references(content)
        assert "config.yaml" in refs

    def test_extract_file_references_no_files_returns_empty(self) -> None:
        """Returns empty list when no file references found."""
        from core.techconfig_verifier import _extract_file_references

        content = "The system uses a centralized gateway pattern"
        refs = _extract_file_references(content)
        assert refs == []


class TestCheckFileExists:
    """Tests for _check_file_exists helper."""

    def test_check_file_exists_true(self) -> None:
        """Returns True for existing file."""
        from core.techconfig_verifier import _check_file_exists

        with patch("os.path.isfile", return_value=True):
            assert _check_file_exists("server.py") is True

    def test_check_file_exists_false(self) -> None:
        """Returns False for non-existing file."""
        from core.techconfig_verifier import _check_file_exists

        with patch("os.path.isfile", return_value=False):
            assert _check_file_exists("nonexistent.py") is False


class TestCheckSymbolInFile:
    """Tests for _check_symbol_in_file helper."""

    def test_check_symbol_in_file_found(self) -> None:
        """Returns True when symbol is found in file."""
        from core.techconfig_verifier import _check_symbol_in_file

        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result):
            assert _check_symbol_in_file("server.py", "FastAPI") is True

    def test_check_symbol_in_file_not_found(self) -> None:
        """Returns False when symbol is not found in file."""
        from core.techconfig_verifier import _check_symbol_in_file

        mock_result = MagicMock()
        mock_result.returncode = 1
        with patch("subprocess.run", return_value=mock_result):
            assert _check_symbol_in_file("server.py", "nonexistent") is False


class TestCheckSymbolInProject:
    """Tests for _check_symbol_in_project helper."""

    def test_check_symbol_in_project_found(self) -> None:
        """Returns True when symbol is found anywhere in project."""
        from core.techconfig_verifier import _check_symbol_in_project

        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result):
            assert _check_symbol_in_project("FastAPI") is True

    def test_check_symbol_in_project_not_found(self) -> None:
        """Returns False when symbol is not found in project."""
        from core.techconfig_verifier import _check_symbol_in_project

        mock_result = MagicMock()
        mock_result.returncode = 1
        with patch("subprocess.run", return_value=mock_result):
            assert _check_symbol_in_project("nonexistent_symbol") is False

    def test_check_symbol_in_project_excludes_non_source_dirs(self) -> None:
        """Grep command includes --exclude-dir flags for .git, backups, etc."""
        from core.techconfig_verifier import _check_symbol_in_project

        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            _check_symbol_in_project("some_symbol")
            args = mock_run.call_args[0][0]
            # The command should include --exclude-dir flags
            assert "--exclude-dir=.git" in args
            assert "--exclude-dir=backups" in args
            assert "--exclude-dir=__pycache__" in args
            assert "--exclude-dir=data" in args
            assert "--exclude-dir=documents" in args


class TestPathTraversalValidation:
    """Tests for M1: path traversal protection on codebase_ref."""

    def test_check_file_exists_rejects_path_traversal(self) -> None:
        """_check_file_exists returns False for path traversal attempts."""
        from core.techconfig_verifier import _check_file_exists

        # ../../etc/passwd resolves outside PROJECT_DIR
        assert _check_file_exists("../../etc/passwd") is False

    def test_check_symbol_in_file_rejects_path_traversal(self) -> None:
        """_check_symbol_in_file returns False for path traversal attempts."""
        from core.techconfig_verifier import _check_symbol_in_file

        assert _check_symbol_in_file("../../etc/passwd", "root") is False

    def test_is_path_within_project_valid_path(self) -> None:
        """_is_path_within_project returns True for paths within PROJECT_DIR."""
        from core.techconfig_verifier import _is_path_within_project

        assert _is_path_within_project("server.py") is True
        assert _is_path_within_project("core/sessions.py") is True

    def test_is_path_within_project_traversal_path(self) -> None:
        """_is_path_within_project returns False for paths escaping PROJECT_DIR."""
        from core.techconfig_verifier import _is_path_within_project

        assert _is_path_within_project("../../etc/passwd") is False
        assert _is_path_within_project("/etc/passwd") is False
        assert _is_path_within_project("../../../tmp/evil") is False

    def test_verify_entry_with_traversal_codebase_ref_returns_flagged(self) -> None:
        """verify_entry returns flagged when codebase_ref is a path traversal."""
        from core.techconfig_verifier import verify_entry

        result = verify_entry(
            content="Some technical config content about password files",
            element_id="elem-traversal",
            codebase_ref="../../etc/passwd",
        )
        assert result.status == "flagged"
        assert "outside" in result.reason.lower() or "traversal" in result.reason.lower()
