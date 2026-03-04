"""Unit tests for core/dep_scan.py — pip-audit wrapper with JSON parsing."""

import json
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from core.dep_scan import (
    ScanResult,
    VulnerabilityRecord,
    parse_pip_audit_output,
    run_pip_audit,
)


class TestParseOutput:
    """Tests for parse_pip_audit_output()."""

    def test_parse_no_vulnerabilities(self) -> None:
        raw = json.dumps({
            "dependencies": [
                {"name": "requests", "version": "2.31.0", "vulns": []},
                {"name": "flask", "version": "3.0.0", "vulns": []},
            ]
        })
        result = parse_pip_audit_output(raw)
        assert result.success is True
        assert result.vulnerabilities == []
        assert result.error is None

    def test_parse_with_vulnerabilities(self) -> None:
        raw = json.dumps({
            "dependencies": [
                {
                    "name": "requests",
                    "version": "2.25.0",
                    "vulns": [
                        {
                            "id": "PYSEC-2023-74",
                            "description": "A vulnerability in requests",
                            "fix_versions": ["2.31.0"],
                        }
                    ],
                }
            ]
        })
        result = parse_pip_audit_output(raw)
        assert result.success is True
        assert len(result.vulnerabilities) == 1
        v = result.vulnerabilities[0]
        assert v.package == "requests"
        assert v.version == "2.25.0"
        assert v.vuln_id == "PYSEC-2023-74"
        assert v.description == "A vulnerability in requests"
        assert v.fix_versions == ["2.31.0"]

    def test_parse_multiple_vulns_same_package(self) -> None:
        raw = json.dumps({
            "dependencies": [
                {
                    "name": "urllib3",
                    "version": "1.25.0",
                    "vulns": [
                        {
                            "id": "PYSEC-2021-108",
                            "description": "First vuln",
                            "fix_versions": ["1.26.5"],
                        },
                        {
                            "id": "PYSEC-2023-212",
                            "description": "Second vuln",
                            "fix_versions": ["2.0.7"],
                        },
                    ],
                }
            ]
        })
        result = parse_pip_audit_output(raw)
        assert result.success is True
        assert len(result.vulnerabilities) == 2
        assert result.vulnerabilities[0].vuln_id == "PYSEC-2021-108"
        assert result.vulnerabilities[1].vuln_id == "PYSEC-2023-212"
        # Both should reference the same package
        assert all(v.package == "urllib3" for v in result.vulnerabilities)

    def test_parse_invalid_json(self) -> None:
        result = parse_pip_audit_output("not valid json {{{")
        assert result.success is False
        assert result.error is not None
        assert "json" in result.error.lower() or "parse" in result.error.lower()

    def test_parse_empty_dependencies(self) -> None:
        raw = json.dumps({"dependencies": []})
        result = parse_pip_audit_output(raw)
        assert result.success is True
        assert result.vulnerabilities == []


class TestScanResultProperties:
    """Tests for ScanResult dataclass properties."""

    def test_has_vulnerabilities_true(self) -> None:
        result = ScanResult(
            success=True,
            vulnerabilities=[
                VulnerabilityRecord(
                    package="pkg",
                    version="1.0",
                    vuln_id="CVE-1",
                    description="test",
                    fix_versions=["2.0"],
                )
            ],
            error=None,
            raw_output="",
        )
        assert result.has_vulnerabilities is True

    def test_has_vulnerabilities_false(self) -> None:
        result = ScanResult(
            success=True,
            vulnerabilities=[],
            error=None,
            raw_output="",
        )
        assert result.has_vulnerabilities is False

    def test_summary_no_vulns(self) -> None:
        result = ScanResult(
            success=True,
            vulnerabilities=[],
            error=None,
            raw_output="",
        )
        assert result.summary == "No known vulnerabilities found."

    def test_summary_with_vulns(self) -> None:
        result = ScanResult(
            success=True,
            vulnerabilities=[
                VulnerabilityRecord(
                    package="requests",
                    version="2.25.0",
                    vuln_id="PYSEC-2023-74",
                    description="test",
                    fix_versions=["2.31.0"],
                ),
                VulnerabilityRecord(
                    package="urllib3",
                    version="1.25.0",
                    vuln_id="PYSEC-2021-108",
                    description="test",
                    fix_versions=["1.26.5"],
                ),
            ],
            error=None,
            raw_output="",
        )
        summary = result.summary
        assert "2" in summary
        assert "requests" in summary
        assert "urllib3" in summary

    def test_summary_on_failure(self) -> None:
        result = ScanResult(
            success=False,
            vulnerabilities=[],
            error="pip-audit crashed",
            raw_output="",
        )
        assert "pip-audit crashed" in result.summary
        assert "failed" in result.summary.lower() or "Scan failed" in result.summary


class TestRunPipAudit:
    """Tests for run_pip_audit() subprocess invocation."""

    @patch("core.dep_scan.subprocess.run")
    def test_calls_subprocess(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"dependencies": []}),
            stderr="",
        )
        run_pip_audit()
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert cmd[0] == sys.executable
        assert "-m" in cmd
        assert "pip_audit" in cmd
        assert "--format=json" in cmd
        assert "--output=-" in cmd
        assert call_args[1].get("shell") is not True  # shell=False or not set

    @patch("core.dep_scan.subprocess.run")
    def test_returns_scan_result_on_success(self, mock_run: MagicMock) -> None:
        raw_json = json.dumps({
            "dependencies": [
                {"name": "flask", "version": "3.0.0", "vulns": []},
            ]
        })
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=raw_json,
            stderr="",
        )
        result = run_pip_audit()
        assert isinstance(result, ScanResult)
        assert result.success is True
        assert result.vulnerabilities == []

    @patch("core.dep_scan.subprocess.run")
    def test_returns_failure_on_nonzero_exit_with_no_json(
        self, mock_run: MagicMock
    ) -> None:
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="pip-audit: error: something went wrong",
        )
        result = run_pip_audit()
        assert result.success is False
        assert result.error is not None

    @patch("core.dep_scan.subprocess.run")
    def test_handles_timeout(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd="pip-audit", timeout=120
        )
        result = run_pip_audit()
        assert result.success is False
        assert "timed out" in result.error.lower()

    @patch("core.dep_scan.subprocess.run")
    def test_handles_file_not_found(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = FileNotFoundError("No such file or directory")
        result = run_pip_audit()
        assert result.success is False
        assert "not installed" in result.error.lower() or "not found" in result.error.lower()

    @patch("core.dep_scan.subprocess.run")
    def test_with_requirements_file(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"dependencies": []}),
            stderr="",
        )
        run_pip_audit(requirements_file="requirements.txt")
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "-r" in cmd
        assert "requirements.txt" in cmd

    @patch("core.dep_scan.subprocess.run")
    def test_nonzero_exit_with_valid_json_parses_vulns(
        self, mock_run: MagicMock
    ) -> None:
        """pip-audit returns exit code 1 when vulnerabilities are found, but still outputs JSON."""
        raw_json = json.dumps({
            "dependencies": [
                {
                    "name": "requests",
                    "version": "2.25.0",
                    "vulns": [
                        {
                            "id": "PYSEC-2023-74",
                            "description": "A vulnerability",
                            "fix_versions": ["2.31.0"],
                        }
                    ],
                }
            ]
        })
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout=raw_json,
            stderr="",
        )
        result = run_pip_audit()
        assert result.success is True
        assert len(result.vulnerabilities) == 1
