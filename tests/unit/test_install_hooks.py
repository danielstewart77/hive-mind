"""Unit tests for scripts/install-hooks.sh — hook installation script."""

import os
import stat
from pathlib import Path

import pytest

SCRIPTS_DIR = Path("/usr/src/app/scripts")
INSTALL_SCRIPT = SCRIPTS_DIR / "install-hooks.sh"


class TestInstallHooks:
    """Tests for the install-hooks.sh script."""

    def test_install_script_exists(self) -> None:
        assert INSTALL_SCRIPT.exists(), f"Install script not found at {INSTALL_SCRIPT}"

    def test_install_script_is_executable(self) -> None:
        mode = INSTALL_SCRIPT.stat().st_mode
        assert mode & stat.S_IXUSR, "Install script should be executable by owner"

    def test_install_script_references_pre_commit_hook(self) -> None:
        content = INSTALL_SCRIPT.read_text()
        assert "pre-commit-pip-audit.sh" in content

    def test_install_script_preserves_existing_hooks(self) -> None:
        content = INSTALL_SCRIPT.read_text()
        # Should check for existing hook and back it up
        assert "bak" in content.lower() or "backup" in content.lower()
