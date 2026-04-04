"""Integration test for soul_nudge.sh — bash syntax validation."""

import shutil
import subprocess

import pytest

SOUL_NUDGE_PATH = "/home/hivemind/.claude/hooks/soul_nudge.sh"


class TestSoulNudgeBashSyntax:
    """Validate soul_nudge.sh has valid bash syntax."""

    def test_soul_nudge_script_valid_bash_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", SOUL_NUDGE_PATH],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Bash syntax error: {result.stderr}"

    @pytest.mark.skipif(
        shutil.which("shellcheck") is None, reason="shellcheck not installed"
    )
    def test_soul_nudge_script_passes_shellcheck(self) -> None:
        """AC: soul_nudge.sh passes shellcheck with no errors."""
        result = subprocess.run(
            ["shellcheck", SOUL_NUDGE_PATH],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"shellcheck errors:\n{result.stdout}"
