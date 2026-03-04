"""Dependency vulnerability scanning via pip-audit.

Provides a Python wrapper around pip-audit that runs it as a subprocess,
parses its JSON output, and returns typed results. Used by the pre-commit
hook and can be run directly as a CLI: ``python core/dep_scan.py``
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class VulnerabilityRecord:
    """A single vulnerability finding from pip-audit."""

    package: str
    version: str
    vuln_id: str
    description: str
    fix_versions: list[str]


@dataclass
class ScanResult:
    """Aggregated result from a pip-audit scan."""

    success: bool
    vulnerabilities: list[VulnerabilityRecord]
    error: str | None
    raw_output: str

    @property
    def has_vulnerabilities(self) -> bool:
        return len(self.vulnerabilities) > 0

    @property
    def summary(self) -> str:
        if not self.success:
            return f"Scan failed: {self.error}"
        if not self.has_vulnerabilities:
            return "No known vulnerabilities found."
        return (
            f"Found {len(self.vulnerabilities)} vulnerability(ies): "
            + ", ".join(
                f"{v.package}=={v.version} ({v.vuln_id})"
                for v in self.vulnerabilities
            )
        )


def parse_pip_audit_output(raw_json: str) -> ScanResult:
    """Parse pip-audit ``--format=json`` output into a ScanResult.

    The JSON structure produced by pip-audit is::

        {"dependencies": [
            {"name": "pkg", "version": "1.0", "vulns": [
                {"id": "...", "description": "...", "fix_versions": [...]}
            ]}
        ]}
    """
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError) as exc:
        return ScanResult(
            success=False,
            vulnerabilities=[],
            error=f"Failed to parse pip-audit JSON output: {exc}",
            raw_output=raw_json,
        )

    vulnerabilities: list[VulnerabilityRecord] = []
    for dep in data.get("dependencies", []):
        for vuln in dep.get("vulns", []):
            vulnerabilities.append(
                VulnerabilityRecord(
                    package=dep["name"],
                    version=dep["version"],
                    vuln_id=vuln.get("id", "UNKNOWN"),
                    description=vuln.get("description", ""),
                    fix_versions=vuln.get("fix_versions", []),
                )
            )

    return ScanResult(
        success=True,
        vulnerabilities=vulnerabilities,
        error=None,
        raw_output=raw_json,
    )


def run_pip_audit(
    requirements_file: str | None = None,
    timeout: int = 120,
) -> ScanResult:
    """Execute pip-audit and return parsed results.

    Args:
        requirements_file: Optional path to a requirements file to scan.
            If None, scans the current environment.
        timeout: Subprocess timeout in seconds (default 120).

    Returns:
        A ScanResult with parsed vulnerability data.
    """
    cmd = [sys.executable, "-m", "pip_audit", "--format=json", "--output=-"]
    if requirements_file is not None:
        cmd.extend(["-r", requirements_file])

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ScanResult(
            success=False,
            vulnerabilities=[],
            error=f"pip-audit timed out after {timeout} seconds",
            raw_output="",
        )
    except FileNotFoundError:
        return ScanResult(
            success=False,
            vulnerabilities=[],
            error="pip-audit is not installed or not found in PATH",
            raw_output="",
        )

    # pip-audit returns exit code 1 when vulnerabilities are found
    # but still produces valid JSON on stdout. Try to parse it.
    stdout = proc.stdout.strip()
    if stdout:
        result = parse_pip_audit_output(stdout)
        if result.success:
            return result

    # If stdout was empty or unparseable, and exit code is non-zero, it's a failure
    if proc.returncode != 0:
        error_msg = proc.stderr.strip() if proc.stderr.strip() else f"pip-audit exited with code {proc.returncode}"
        return ScanResult(
            success=False,
            vulnerabilities=[],
            error=error_msg,
            raw_output=stdout,
        )

    # Exit code 0, no stdout — treat as clean
    return parse_pip_audit_output(stdout if stdout else '{"dependencies": []}')


def main() -> int:
    """CLI entry point. Returns 0 if clean, 1 if vulnerabilities found or error."""
    result = run_pip_audit()
    print(result.summary)
    if result.has_vulnerabilities:
        for v in result.vulnerabilities:
            fix = ", ".join(v.fix_versions) if v.fix_versions else "no fix available"
            print(f"  {v.package}=={v.version}: {v.vuln_id} (fix: {fix})")
        return 1
    if not result.success:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
