"""Unit tests for core/dep_scan.py CLI entry point (main function)."""

from io import StringIO
from unittest.mock import patch

import pytest

from core.dep_scan import ScanResult, VulnerabilityRecord, main


class TestDepScanCLI:
    """Tests for the main() CLI entry point."""

    @patch("core.dep_scan.run_pip_audit")
    def test_exits_zero_on_clean_scan(self, mock_run: object) -> None:
        mock_run.return_value = ScanResult(  # type: ignore[union-attr]
            success=True,
            vulnerabilities=[],
            error=None,
            raw_output="{}",
        )
        assert main() == 0

    @patch("core.dep_scan.run_pip_audit")
    def test_exits_one_on_vulnerabilities(self, mock_run: object) -> None:
        mock_run.return_value = ScanResult(  # type: ignore[union-attr]
            success=True,
            vulnerabilities=[
                VulnerabilityRecord(
                    package="requests",
                    version="2.25.0",
                    vuln_id="PYSEC-2023-74",
                    description="A vulnerability",
                    fix_versions=["2.31.0"],
                )
            ],
            error=None,
            raw_output="{}",
        )
        assert main() == 1

    @patch("core.dep_scan.run_pip_audit")
    def test_exits_one_on_scan_failure(self, mock_run: object) -> None:
        mock_run.return_value = ScanResult(  # type: ignore[union-attr]
            success=False,
            vulnerabilities=[],
            error="pip-audit crashed",
            raw_output="",
        )
        assert main() == 1

    @patch("core.dep_scan.run_pip_audit")
    def test_prints_summary(self, mock_run: object) -> None:
        mock_run.return_value = ScanResult(  # type: ignore[union-attr]
            success=True,
            vulnerabilities=[],
            error=None,
            raw_output="{}",
        )
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            main()
            output = mock_stdout.getvalue()
            assert "No known vulnerabilities found." in output

    @patch("core.dep_scan.run_pip_audit")
    def test_prints_vulnerability_details(self, mock_run: object) -> None:
        mock_run.return_value = ScanResult(  # type: ignore[union-attr]
            success=True,
            vulnerabilities=[
                VulnerabilityRecord(
                    package="requests",
                    version="2.25.0",
                    vuln_id="PYSEC-2023-74",
                    description="A vulnerability",
                    fix_versions=["2.31.0"],
                )
            ],
            error=None,
            raw_output="{}",
        )
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            main()
            output = mock_stdout.getvalue()
            assert "requests" in output
            assert "PYSEC-2023-74" in output
            assert "2.31.0" in output
