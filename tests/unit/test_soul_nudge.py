"""Unit tests for soul_nudge.sh — Stop hook that triggers soul load/reflect cycles.

Tests the shell script's branching logic:
- Turn 1: emits --load only (identity bootstrap)
- Nudge turns (every 5): emits --load then --reflect
- Regular turns: no output
- Group sessions: suppressed
"""

import os
import stat
import subprocess
import tempfile
from pathlib import Path

SCRIPT_PATH = Path("/home/hivemind/.claude/hooks/soul_nudge.sh")


class TestSoulNudgeScriptExists:
    """Basic file and permission checks."""

    def test_soul_nudge_script_exists(self) -> None:
        assert SCRIPT_PATH.exists(), f"Script not found at {SCRIPT_PATH}"

    def test_soul_nudge_script_is_executable(self) -> None:
        mode = SCRIPT_PATH.stat().st_mode
        assert mode & stat.S_IXUSR, "Script should be executable by owner"


class TestSoulNudgeTurn1Load:
    """Turn 1 should emit --load only (identity bootstrap, no reflect)."""

    def test_soul_nudge_emits_load_on_turn_1(self) -> None:
        """Run the script with counter file starting at 0 -> count becomes 1."""
        with tempfile.TemporaryDirectory() as tmpdir:
            counter_file = os.path.join(tmpdir, "counter")
            with open(counter_file, "w") as f:
                f.write("0")

            env = os.environ.copy()
            env.pop("HIVEMIND_GROUP_SESSION", None)
            env["COUNTER_FILE"] = counter_file
            result = subprocess.run(
                ["bash", "-c", f'COUNTER_FILE="{counter_file}" source "{SCRIPT_PATH}"'],
                capture_output=True,
                text=True,
                env=env,
            )
            assert "/self-reflect --load" in result.stderr, (
                f"Expected --load on turn 1, got stderr: {result.stderr!r}"
            )
            assert "/self-reflect --reflect" not in result.stderr, (
                f"Turn 1 should NOT emit --reflect, got stderr: {result.stderr!r}"
            )

    def test_soul_nudge_exit_code_2_on_turn_1(self) -> None:
        """Exit code must be 2 on turn 1 for Claude Code to process the output."""
        with tempfile.TemporaryDirectory() as tmpdir:
            counter_file = os.path.join(tmpdir, "counter")
            with open(counter_file, "w") as f:
                f.write("0")

            env = os.environ.copy()
            env.pop("HIVEMIND_GROUP_SESSION", None)
            env["COUNTER_FILE"] = counter_file
            result = subprocess.run(
                ["bash", "-c", f'COUNTER_FILE="{counter_file}" source "{SCRIPT_PATH}"'],
                capture_output=True,
                text=True,
                env=env,
            )
            assert result.returncode == 2, (
                f"Expected exit code 2 on turn 1, got {result.returncode}"
            )


class TestSoulNudgeNudgeTurn:
    """Nudge turns (every NUDGE_EVERY) should emit --load then --reflect."""

    def test_soul_nudge_emits_load_then_reflect_on_nudge_turn(self) -> None:
        """Counter at 4 -> count becomes 5 (nudge turn). Should emit both."""
        with tempfile.TemporaryDirectory() as tmpdir:
            counter_file = os.path.join(tmpdir, "counter")
            with open(counter_file, "w") as f:
                f.write("4")

            env = os.environ.copy()
            env.pop("HIVEMIND_GROUP_SESSION", None)
            env["COUNTER_FILE"] = counter_file
            result = subprocess.run(
                ["bash", "-c", f'COUNTER_FILE="{counter_file}" source "{SCRIPT_PATH}"'],
                capture_output=True,
                text=True,
                env=env,
            )
            assert "/self-reflect --load" in result.stderr, (
                f"Expected --load on nudge turn, got stderr: {result.stderr!r}"
            )
            assert "/self-reflect --reflect" in result.stderr, (
                f"Expected --reflect on nudge turn, got stderr: {result.stderr!r}"
            )
            # --load must come before --reflect
            load_pos = result.stderr.index("/self-reflect --load")
            reflect_pos = result.stderr.index("/self-reflect --reflect")
            assert load_pos < reflect_pos, (
                "--load must come before --reflect in output"
            )

    def test_soul_nudge_exit_code_2_on_nudge_turn(self) -> None:
        """Exit code must be 2 on nudge turn."""
        with tempfile.TemporaryDirectory() as tmpdir:
            counter_file = os.path.join(tmpdir, "counter")
            with open(counter_file, "w") as f:
                f.write("4")

            env = os.environ.copy()
            env.pop("HIVEMIND_GROUP_SESSION", None)
            env["COUNTER_FILE"] = counter_file
            result = subprocess.run(
                ["bash", "-c", f'COUNTER_FILE="{counter_file}" source "{SCRIPT_PATH}"'],
                capture_output=True,
                text=True,
                env=env,
            )
            assert result.returncode == 2, (
                f"Expected exit code 2 on nudge turn, got {result.returncode}"
            )


class TestSoulNudgeRegularTurn:
    """Regular turns (not turn 1, not nudge) should produce no output."""

    def test_soul_nudge_no_output_on_regular_turn(self) -> None:
        """Counter at 1 -> count becomes 2 (not turn 1, not nudge). No output."""
        with tempfile.TemporaryDirectory() as tmpdir:
            counter_file = os.path.join(tmpdir, "counter")
            with open(counter_file, "w") as f:
                f.write("1")

            env = os.environ.copy()
            env.pop("HIVEMIND_GROUP_SESSION", None)
            env["COUNTER_FILE"] = counter_file
            result = subprocess.run(
                ["bash", "-c", f'COUNTER_FILE="{counter_file}" source "{SCRIPT_PATH}"'],
                capture_output=True,
                text=True,
                env=env,
            )
            assert result.stderr == "", (
                f"Expected no stderr on regular turn, got: {result.stderr!r}"
            )
            assert result.returncode == 0, (
                f"Expected exit code 0 on regular turn, got {result.returncode}"
            )


class TestSoulNudgeGroupSessionSuppressed:
    """Group sessions suppress all output."""

    def test_soul_nudge_suppressed_in_group_session(self) -> None:
        """With HIVEMIND_GROUP_SESSION=1, no output regardless of turn count."""
        with tempfile.TemporaryDirectory() as tmpdir:
            counter_file = os.path.join(tmpdir, "counter")
            with open(counter_file, "w") as f:
                f.write("0")

            env = os.environ.copy()
            env["HIVEMIND_GROUP_SESSION"] = "1"
            env["COUNTER_FILE"] = counter_file
            result = subprocess.run(
                ["bash", "-c", f'COUNTER_FILE="{counter_file}" source "{SCRIPT_PATH}"'],
                capture_output=True,
                text=True,
                env=env,
            )
            assert result.returncode == 0, (
                f"Expected exit code 0 in group session, got {result.returncode}"
            )
            assert result.stderr == "", (
                f"Expected no stderr in group session, got: {result.stderr!r}"
            )


class TestSoulNudgeRegressions:
    """Regression tests — verify existing behavior is preserved."""

    def test_soul_nudge_counter_file_increments(self) -> None:
        """Running the script twice should increment the counter to 2."""
        with tempfile.TemporaryDirectory() as tmpdir:
            counter_file = os.path.join(tmpdir, "counter")
            with open(counter_file, "w") as f:
                f.write("0")

            env = os.environ.copy()
            env.pop("HIVEMIND_GROUP_SESSION", None)
            env["COUNTER_FILE"] = counter_file

            # Run once (count goes to 1)
            subprocess.run(
                ["bash", "-c", f'COUNTER_FILE="{counter_file}" source "{SCRIPT_PATH}"'],
                capture_output=True,
                text=True,
                env=env,
            )
            # Run again (count goes to 2)
            subprocess.run(
                ["bash", "-c", f'COUNTER_FILE="{counter_file}" source "{SCRIPT_PATH}"'],
                capture_output=True,
                text=True,
                env=env,
            )
            with open(counter_file) as f:
                value = f.read().strip()
            assert value == "2", f"Expected counter to be 2 after two runs, got {value!r}"

    def test_soul_nudge_preserves_nudge_interval(self) -> None:
        """The script should still use NUDGE_EVERY=5."""
        content = SCRIPT_PATH.read_text()
        assert "NUDGE_EVERY=5" in content, "Nudge interval should be 5"

    def test_soul_nudge_reflect_still_fires_on_nudge_turn(self) -> None:
        """On nudge turn (count=5), --reflect still fires (regression guard)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            counter_file = os.path.join(tmpdir, "counter")
            with open(counter_file, "w") as f:
                f.write("4")

            env = os.environ.copy()
            env.pop("HIVEMIND_GROUP_SESSION", None)
            env["COUNTER_FILE"] = counter_file
            result = subprocess.run(
                ["bash", "-c", f'COUNTER_FILE="{counter_file}" source "{SCRIPT_PATH}"'],
                capture_output=True,
                text=True,
                env=env,
            )
            assert "/self-reflect --reflect" in result.stderr, (
                f"Expected --reflect on nudge turn, got stderr: {result.stderr!r}"
            )
