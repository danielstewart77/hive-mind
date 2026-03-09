"""Path validation for skill agent document paths.

Prevents CWE-22 path traversal by ensuring all documents_path values
resolve to a subdirectory within the project's docs/ directory.
Uses os.path.realpath() to canonicalize paths and resolve symlinks.
"""

import os

from config import PROJECT_DIR

DOCUMENTS_DIR = PROJECT_DIR / "docs"


def validate_documents_path(documents_path: str) -> str:
    """Validate and canonicalize a documents_path.

    Returns the resolved (canonicalized) path string if valid.
    Raises ValueError if the path is empty, contains null bytes,
    or resolves to a location outside the allowed docs/ directory.

    Args:
        documents_path: The raw path string to validate.

    Returns:
        The resolved, canonical path as a string.

    Raises:
        ValueError: If the path is invalid or escapes the allowed directory.
    """
    if not documents_path:
        raise ValueError("documents_path must not be empty")

    if "\x00" in documents_path:
        raise ValueError("documents_path must not contain null bytes")

    resolved = os.path.realpath(documents_path)
    required_prefix = str(DOCUMENTS_DIR) + os.sep

    if not resolved.startswith(required_prefix):
        raise ValueError(
            "documents_path is outside the allowed docs/ directory"
        )

    return resolved
