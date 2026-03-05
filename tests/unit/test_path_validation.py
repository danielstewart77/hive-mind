"""Unit tests for core/path_validation.py -- path traversal prevention."""

import os
import tempfile

import pytest

from config import PROJECT_DIR
from core.path_validation import validate_documents_path


DOCUMENTS_DIR = PROJECT_DIR / "documents"


class TestValidPathsAccepted:
    """Tests that valid paths within documents/ are accepted."""

    def test_valid_path_within_documents_returns_resolved(self) -> None:
        valid_path = str(DOCUMENTS_DIR / "12345")
        result = validate_documents_path(valid_path)
        assert result == os.path.realpath(valid_path)

    def test_valid_path_with_subdirectory_returns_resolved(self) -> None:
        valid_path = str(DOCUMENTS_DIR / "12345" / "sub")
        result = validate_documents_path(valid_path)
        assert result == os.path.realpath(valid_path)

    def test_return_type_is_string(self) -> None:
        valid_path = str(DOCUMENTS_DIR / "12345")
        result = validate_documents_path(valid_path)
        assert isinstance(result, str)


class TestTraversalAttacksRejected:
    """Tests that path traversal attacks are blocked."""

    def test_relative_traversal_dot_dot_rejected(self) -> None:
        malicious = str(DOCUMENTS_DIR / ".." / ".env")
        with pytest.raises(ValueError, match="outside the allowed"):
            validate_documents_path(malicious)

    def test_deep_traversal_rejected(self) -> None:
        malicious = str(DOCUMENTS_DIR / ".." / ".." / "etc" / "shadow")
        with pytest.raises(ValueError, match="outside the allowed"):
            validate_documents_path(malicious)

    def test_absolute_path_outside_documents_rejected(self) -> None:
        with pytest.raises(ValueError, match="outside the allowed"):
            validate_documents_path("/etc/shadow")

    def test_absolute_path_to_env_rejected(self) -> None:
        malicious = str(PROJECT_DIR / ".env")
        with pytest.raises(ValueError, match="outside the allowed"):
            validate_documents_path(malicious)


class TestSymlinkEscapeRejected:
    """Tests that symlink-based escapes are blocked."""

    def test_symlink_escape_rejected(self, tmp_path: object) -> None:
        """Create a symlink inside documents/ pointing outside, verify rejection."""
        # Create a real temp dir to symlink to
        with tempfile.TemporaryDirectory() as escape_target:
            # Create the symlink inside the real documents/ dir
            symlink_path = DOCUMENTS_DIR / "evil_symlink_test"
            try:
                os.symlink(escape_target, str(symlink_path))
                with pytest.raises(ValueError, match="outside the allowed"):
                    validate_documents_path(str(symlink_path))
            finally:
                # Clean up the symlink
                if symlink_path.exists() or symlink_path.is_symlink():
                    os.unlink(str(symlink_path))


class TestEdgeCasesRejected:
    """Tests for edge cases and malformed inputs."""

    def test_empty_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            validate_documents_path("")

    def test_documents_dir_itself_rejected(self) -> None:
        with pytest.raises(ValueError, match="outside the allowed"):
            validate_documents_path(str(DOCUMENTS_DIR))

    def test_path_with_null_byte_rejected(self) -> None:
        malicious = str(DOCUMENTS_DIR / "12345") + "\x00"
        with pytest.raises(ValueError, match="null"):
            validate_documents_path(malicious)

    def test_error_message_does_not_leak_resolved_path(self) -> None:
        """Verify error message says 'outside allowed directory' but does not
        contain the full resolved path (prevents information leakage)."""
        try:
            validate_documents_path("/etc/shadow")
        except ValueError as e:
            error_msg = str(e)
            assert "outside the allowed" in error_msg
            assert "/etc/shadow" not in error_msg
