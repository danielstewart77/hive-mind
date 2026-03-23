"""Unit tests for the stateless agent_logs tool."""

import json
import os
import subprocess
import sys
import tempfile


SCRIPT_PATH = "/usr/src/app/tools/stateless/agent_logs/agent_logs.py"


class TestStatelessAgentLogs:
    def test_no_critical_entries_returns_ok(self):
        """Asserts status ok when log files contain no critical entries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "clean.log")
            pos_file = os.path.join(tmpdir, "positions")
            with open(log_file, "w") as f:
                f.write("INFO: all good\nDEBUG: nothing to see\n")

            result = subprocess.run(
                [sys.executable, SCRIPT_PATH,
                 "--log-paths", log_file,
                 "--pos-file", pos_file],
                capture_output=True, text=True, timeout=10,
            )
            data = json.loads(result.stdout)
            assert data["status"] == "ok"
            assert result.returncode == 0

    def test_detects_critical_entries(self):
        """Asserts critical entries found when log contains error/failure lines."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "errors.log")
            pos_file = os.path.join(tmpdir, "positions")
            with open(log_file, "w") as f:
                f.write("INFO: starting up\n")
                f.write("ERROR: disk full\n")
                f.write("CRITICAL: out of memory\n")
                f.write("INFO: recovered\n")

            result = subprocess.run(
                [sys.executable, SCRIPT_PATH,
                 "--log-paths", log_file,
                 "--pos-file", pos_file],
                capture_output=True, text=True, timeout=10,
            )
            data = json.loads(result.stdout)
            assert data["status"] == "critical"
            assert log_file in data["findings"]
            assert len(data["findings"][log_file]) == 2

    def test_tracks_position_across_calls(self):
        """Asserts position tracking so only new entries are reported."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "tracked.log")
            pos_file = os.path.join(tmpdir, "positions")

            # First write and scan
            with open(log_file, "w") as f:
                f.write("ERROR: first error\n")

            subprocess.run(
                [sys.executable, SCRIPT_PATH,
                 "--log-paths", log_file,
                 "--pos-file", pos_file],
                capture_output=True, text=True, timeout=10,
            )

            # Second scan should show no new entries
            result = subprocess.run(
                [sys.executable, SCRIPT_PATH,
                 "--log-paths", log_file,
                 "--pos-file", pos_file],
                capture_output=True, text=True, timeout=10,
            )
            data = json.loads(result.stdout)
            assert data["status"] == "ok"

            # Append a new error and scan again
            with open(log_file, "a") as f:
                f.write("FAILURE: second error\n")

            result = subprocess.run(
                [sys.executable, SCRIPT_PATH,
                 "--log-paths", log_file,
                 "--pos-file", pos_file],
                capture_output=True, text=True, timeout=10,
            )
            data = json.loads(result.stdout)
            assert data["status"] == "critical"
            assert len(data["findings"][log_file]) == 1

    def test_missing_log_file_skipped(self):
        """Asserts non-existent log files are silently skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pos_file = os.path.join(tmpdir, "positions")
            result = subprocess.run(
                [sys.executable, SCRIPT_PATH,
                 "--log-paths", "/nonexistent/log.log",
                 "--pos-file", pos_file],
                capture_output=True, text=True, timeout=10,
            )
            data = json.loads(result.stdout)
            assert data["status"] == "ok"
            assert result.returncode == 0

    def test_multiple_log_files(self):
        """Asserts multiple log files scanned and results grouped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log1 = os.path.join(tmpdir, "syslog")
            log2 = os.path.join(tmpdir, "kern.log")
            pos_file = os.path.join(tmpdir, "positions")

            with open(log1, "w") as f:
                f.write("ERROR: sys error\n")
            with open(log2, "w") as f:
                f.write("panic: kernel panic\n")

            result = subprocess.run(
                [sys.executable, SCRIPT_PATH,
                 "--log-paths", f"{log1},{log2}",
                 "--pos-file", pos_file],
                capture_output=True, text=True, timeout=10,
            )
            data = json.loads(result.stdout)
            assert data["status"] == "critical"
            assert log1 in data["findings"]
            assert log2 in data["findings"]

    def test_exit_code_zero_on_success(self):
        """Asserts exit code 0 regardless of findings."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "test.log")
            pos_file = os.path.join(tmpdir, "positions")
            with open(log_file, "w") as f:
                f.write("ERROR: something\n")

            result = subprocess.run(
                [sys.executable, SCRIPT_PATH,
                 "--log-paths", log_file,
                 "--pos-file", pos_file],
                capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0
