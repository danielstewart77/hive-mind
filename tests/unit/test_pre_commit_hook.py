"""Unit tests for scripts/pre-commit-pip-audit.sh — pre-commit hook script."""

import os
import stat
from pathlib import Path

import pytest

SCRIPTS_DIR = Path("/usr/src/app/scripts")
HOOK_SCRIPT = SCRIPTS_DIR / "pre-commit-pip-audit.sh"


class TestPreCommitHook:
    """Tests for the pre-commit pip-audit hook script."""

    def test_hook_script_exists(self) -> None:
        assert HOOK_SCRIPT.exists(), f"Hook script not found at {HOOK_SCRIPT}"

    def test_hook_script_is_executable(self) -> None:
        mode = HOOK_SCRIPT.stat().st_mode
        assert mode & stat.S_IXUSR, "Hook script should be executable by owner"

    def test_hook_script_checks_staged_requirements_files(self) -> None:
        content = HOOK_SCRIPT.read_text()
        assert "git diff --cached --name-only" in content
        assert "requirements" in content.lower()

    def test_hook_script_invokes_dep_scan_module(self) -> None:
        content = HOOK_SCRIPT.read_text()
        # Should call dep_scan.py either directly or via python -m
        assert "dep_scan" in content

    def test_hook_script_exits_zero_on_no_requirements_changes(self) -> None:
        content = HOOK_SCRIPT.read_text()
        # Should have a conditional that exits 0 when no requirements files changed
        assert "exit 0" in content
        # Should check if STAGED_REQ is empty
        assert "-z" in content or "empty" in content.lower()
