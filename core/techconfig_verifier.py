"""Technical-config memory entry verifier.

Verifies whether a stored technical-config memory entry is still accurate
by checking the codebase for file existence and symbol presence.

Uses lightweight heuristics only -- no Claude session spawning.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_DIR = Path("/usr/src/app")

# Regex to find file paths in content (e.g. server.py, core/sessions.py, config.yaml)
_FILE_REF_PATTERN = re.compile(
    r"(?:^|[\s`\"'(,])("
    r"(?:[\w./-]+/)?"        # optional directory prefix
    r"[\w.-]+"               # filename stem
    r"\."                    # dot separator
    r"(?:py|yaml|yml|json|toml|cfg|txt|md|sh)"  # known extensions
    r")(?:[\s`\"'),.:;]|$)",
    re.MULTILINE,
)

# Regex to find likely symbol names: function names (word_word), class names (CamelCase),
# config keys (UPPER_CASE), dotted references (module.attr)
_KEYWORD_PATTERN = re.compile(
    r"\b("
    r"[a-z_][a-z0-9_]{2,}"     # snake_case identifiers (min 3 chars)
    r"|[A-Z][a-zA-Z0-9]{2,}"   # CamelCase or UPPER identifiers
    r"|[A-Z_]{3,}"             # UPPER_CASE constants
    r")\b"
)

# Common English words to exclude from keyword extraction
_STOP_WORDS = frozenset({
    "the", "and", "for", "that", "this", "with", "from", "are", "was",
    "were", "been", "have", "has", "had", "not", "but", "all", "can",
    "will", "each", "which", "when", "what", "where", "how", "use",
    "uses", "used", "using", "set", "get", "run", "runs", "new",
    "also", "via", "its", "into", "one", "two", "any", "per",
    "does", "file", "files", "name", "path", "true", "false",
    "none", "null", "default", "value", "values", "type", "types",
    "class", "def", "return", "import", "module", "function",
})


@dataclass
class VerificationResult:
    """Result of verifying a single technical-config memory entry."""

    status: str          # "verified" | "pruned" | "flagged"
    reason: str          # Human-readable explanation
    content: str         # Original memory content
    element_id: str      # Neo4j element ID
    codebase_ref: str | None  # Original codebase_ref if any


def _extract_file_references(content: str) -> list[str]:
    """Extract file path references from content text.

    Finds patterns like server.py, core/sessions.py, config.yaml.

    Args:
        content: The text content to scan.

    Returns:
        List of file path strings found in the content.
    """
    matches = _FILE_REF_PATTERN.findall(content)
    # Deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for match in matches:
        if match not in seen:
            seen.add(match)
            result.append(match)
    return result


def _extract_keywords(content: str) -> list[str]:
    """Extract likely symbol names from content text.

    Pulls function names, class names, config keys, etc.

    Args:
        content: The text content to scan.

    Returns:
        List of keyword strings found in the content.
    """
    matches = _KEYWORD_PATTERN.findall(content)
    # Filter out common stop words and short matches
    seen: set[str] = set()
    result: list[str] = []
    for match in matches:
        lower = match.lower()
        if lower not in _STOP_WORDS and match not in seen:
            seen.add(match)
            result.append(match)
    return result


def _is_path_within_project(filepath: str) -> bool:
    """Check that a resolved path stays within PROJECT_DIR.

    Prevents CWE-22 path traversal by resolving symlinks and relative
    segments, then asserting the result is under PROJECT_DIR.

    Args:
        filepath: Relative path from project root.

    Returns:
        True if the resolved path is within PROJECT_DIR, False otherwise.
    """
    resolved = os.path.realpath(PROJECT_DIR / filepath)
    return resolved.startswith(str(PROJECT_DIR) + os.sep) or resolved == str(PROJECT_DIR)


def _check_file_exists(filepath: str) -> bool:
    """Check if a file exists relative to the project directory.

    Args:
        filepath: Relative path from project root.

    Returns:
        True if the file exists, False otherwise.
        Returns False if the path resolves outside PROJECT_DIR.
    """
    if not _is_path_within_project(filepath):
        logger.warning("Path traversal blocked in _check_file_exists: %s", filepath)
        return False
    return os.path.isfile(PROJECT_DIR / filepath)


def _check_symbol_in_file(filepath: str, symbol: str) -> bool:
    """Check if a symbol appears in a specific file using grep.

    Args:
        filepath: Relative path from project root.
        symbol: The symbol string to search for.

    Returns:
        True if the symbol is found in the file, False otherwise.
        Returns False if the path resolves outside PROJECT_DIR.
    """
    if not _is_path_within_project(filepath):
        logger.warning("Path traversal blocked in _check_symbol_in_file: %s", filepath)
        return False
    try:
        result = subprocess.run(
            ["grep", "-q", symbol, str(PROJECT_DIR / filepath)],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        logger.warning("Symbol check failed for %s in %s", symbol, filepath)
        return False


def _check_symbol_in_project(symbol: str) -> bool:
    """Check if a symbol appears anywhere in the project using recursive grep.

    Args:
        symbol: The symbol string to search for.

    Returns:
        True if the symbol is found anywhere in the project, False otherwise.
    """
    try:
        result = subprocess.run(
            [
                "grep", "-rq",
                "--exclude-dir=.git",
                "--exclude-dir=backups",
                "--exclude-dir=data",
                "--exclude-dir=documents",
                "--exclude-dir=__pycache__",
                "--exclude-dir=.pylibs",
                "--exclude-dir=node_modules",
                symbol,
                str(PROJECT_DIR),
            ],
            capture_output=True,
            timeout=30,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        logger.warning("Project-wide symbol check failed for %s", symbol)
        return False


def verify_entry(
    content: str,
    element_id: str,
    codebase_ref: str | None,
) -> VerificationResult:
    """Verify a single technical-config memory entry against the codebase.

    Decision tree:
    1. If codebase_ref present and file exists: grep for keywords -> verified or pruned
    2. If codebase_ref present and file missing: flagged
    3. If codebase_ref absent: try to infer file from content, then grep keywords
       If no inference possible, check project-wide; if still nothing, flagged

    Args:
        content: The memory entry content text.
        element_id: The Neo4j element ID.
        codebase_ref: Optional file path reference.

    Returns:
        VerificationResult with status, reason, and metadata.
    """
    # Edge case: empty content
    if not content or not content.strip():
        return VerificationResult(
            status="flagged",
            reason="Empty content -- cannot verify",
            content=content,
            element_id=element_id,
            codebase_ref=codebase_ref,
        )

    keywords = _extract_keywords(content)

    if codebase_ref:
        # Validate codebase_ref does not escape PROJECT_DIR
        if not _is_path_within_project(codebase_ref):
            logger.warning("Path traversal blocked for codebase_ref: %s", codebase_ref)
            return VerificationResult(
                status="flagged",
                reason=f"codebase_ref resolves outside project directory (path traversal blocked): {codebase_ref}",
                content=content,
                element_id=element_id,
                codebase_ref=codebase_ref,
            )

        # Case 1: codebase_ref provided
        if not _check_file_exists(codebase_ref):
            return VerificationResult(
                status="flagged",
                reason=f"Referenced file does not exist: {codebase_ref}",
                content=content,
                element_id=element_id,
                codebase_ref=codebase_ref,
            )

        # File exists -- check if keywords are present
        if keywords:
            found_any = any(
                _check_symbol_in_file(codebase_ref, kw) for kw in keywords
            )
            if found_any:
                return VerificationResult(
                    status="verified",
                    reason=f"File {codebase_ref} exists and keywords found",
                    content=content,
                    element_id=element_id,
                    codebase_ref=codebase_ref,
                )
            else:
                return VerificationResult(
                    status="pruned",
                    reason=f"File {codebase_ref} exists but keywords not found in file",
                    content=content,
                    element_id=element_id,
                    codebase_ref=codebase_ref,
                )
        else:
            # No keywords to check, but file exists -- verified (file reference alone is sufficient)
            return VerificationResult(
                status="verified",
                reason=f"File {codebase_ref} exists (no keywords to verify)",
                content=content,
                element_id=element_id,
                codebase_ref=codebase_ref,
            )

    # Case 2: no codebase_ref -- try to infer file from content
    inferred_files = _extract_file_references(content)

    if inferred_files:
        # Try each inferred file
        for filepath in inferred_files:
            if _check_file_exists(filepath):
                if keywords:
                    found_any = any(
                        _check_symbol_in_file(filepath, kw) for kw in keywords
                    )
                    if found_any:
                        return VerificationResult(
                            status="verified",
                            reason=f"Inferred file {filepath} exists and keywords found",
                            content=content,
                            element_id=element_id,
                            codebase_ref=codebase_ref,
                        )
                else:
                    # File exists but no keywords to check
                    return VerificationResult(
                        status="verified",
                        reason=f"Inferred file {filepath} exists (no keywords to verify)",
                        content=content,
                        element_id=element_id,
                        codebase_ref=codebase_ref,
                    )

        # All inferred files either don't exist or keywords not found
        if keywords:
            return VerificationResult(
                status="pruned",
                reason="Inferred file(s) found but keywords not present",
                content=content,
                element_id=element_id,
                codebase_ref=codebase_ref,
            )

    # Case 3: no codebase_ref, no file inference -- check project-wide
    if keywords:
        found_any = any(_check_symbol_in_project(kw) for kw in keywords)
        if found_any:
            return VerificationResult(
                status="verified",
                reason="No file reference but keywords found in project",
                content=content,
                element_id=element_id,
                codebase_ref=codebase_ref,
            )

    # Nothing matched -- flag for human review
    return VerificationResult(
        status="flagged",
        reason="Cannot verify: no file reference and keywords not found in project",
        content=content,
        element_id=element_id,
        codebase_ref=codebase_ref,
    )
