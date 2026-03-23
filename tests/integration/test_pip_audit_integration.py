"""Integration tests for the pip-audit scanning workflow."""

import json
import subprocess
from unittest.mock import MagicMock, patch



class TestDepScanModuleImports:
    """Test that the dep_scan module imports cleanly."""

    def test_dep_scan_module_imports_cleanly(self) -> None:
        from core.dep_scan import ScanResult, VulnerabilityRecord, run_pip_audit

        assert ScanResult is not None
        assert VulnerabilityRecord is not None
        assert run_pip_audit is not None


class TestDepScanParseRoundTrip:
    """Test full parse round-trip with constructed JSON."""

    def test_parse_round_trip(self) -> None:
        from core.dep_scan import parse_pip_audit_output

        raw = json.dumps({
            "dependencies": [
                {
                    "name": "requests",
                    "version": "2.25.0",
                    "vulns": [
                        {
                            "id": "PYSEC-2023-74",
                            "description": "URL redirect vuln",
                            "fix_versions": ["2.31.0"],
                        }
                    ],
                },
                {"name": "flask", "version": "3.0.0", "vulns": []},
            ]
        })
        result = parse_pip_audit_output(raw)
        assert result.success is True
        assert len(result.vulnerabilities) == 1
        v = result.vulnerabilities[0]
        assert v.package == "requests"
        assert v.version == "2.25.0"
        assert v.vuln_id == "PYSEC-2023-74"
        assert v.description == "URL redirect vuln"
        assert v.fix_versions == ["2.31.0"]
        assert result.raw_output == raw


class TestBashScriptSyntax:
    """Test that bash scripts have valid syntax."""

    def test_pre_commit_hook_script_is_valid_bash(self) -> None:
        result = subprocess.run(
            ["bash", "-n", "/usr/src/app/scripts/pre-commit-pip-audit.sh"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Bash syntax error: {result.stderr}"

    def test_install_hooks_script_is_valid_bash(self) -> None:
        result = subprocess.run(
            ["bash", "-n", "/usr/src/app/scripts/install-hooks.sh"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Bash syntax error: {result.stderr}"


class TestDepScanMainReturnType:
    """Test that main() returns an int."""

    @patch("core.dep_scan.subprocess.run")
    def test_main_returns_int(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"dependencies": []}),
            stderr="",
        )
        from core.dep_scan import main

        result = main()
        assert isinstance(result, int)
