# Baseline pip-audit Scan Results

**Date:** 2026-03-03
**Python version:** 3.12.3
**pip-audit version:** 2.10.0
**Environment scanned:** dev venv (.devvenv with dev-only dependencies)

## Summary

Found 2 known vulnerabilities in 1 package (`pip==24.0`).

## Findings

### 1. CVE-2025-8869 -- pip tar archive symlink traversal

| Field | Value |
|-------|-------|
| Package | pip |
| Installed | 24.0 |
| Fix version | 25.3 |
| Aliases | GHSA-4xh5-x5gv-qwph, BIT-pip-2025-8869 |

**Description:** When extracting a tar archive, pip may not check symbolic links
point into the extraction directory if the tarfile module does not implement PEP 706.
Mitigated by using Python >=3.12, which implements PEP 706.

**Risk assessment:** LOW. The project runs Python 3.12.3 which implements PEP 706,
so the vulnerable fallback code is not used.

### 2. CVE-2026-1703 -- pip wheel archive path traversal

| Field | Value |
|-------|-------|
| Package | pip |
| Installed | 24.0 |
| Fix version | 26.0 |
| Aliases | BIT-pip-2026-1703, GHSA-6vgw-5pg2-w6jp |

**Description:** When pip is installing and extracting a maliciously crafted wheel
archive, files may be extracted outside the installation directory. The path traversal
is limited to prefixes of the installation directory.

**Risk assessment:** LOW. The project installs packages from PyPI (trusted source)
inside Docker containers with read-only production volumes. Exploitation requires
a malicious wheel on PyPI.

## Remediation

Both vulnerabilities are in the `pip` package itself (dev tooling, not a runtime
dependency). The production container uses `/opt/venv` which is read-only at
runtime. Recommended action:

- Upgrade pip in dev environments: `pip install --upgrade pip>=26.0`
- The production container's pip version is controlled by the Dockerfile base image
- Neither vulnerability affects runtime application code

## Production Dependencies

The production `requirements.txt` dependencies were not scanned separately in this
baseline (pip-audit is a dev dependency). A scan of the production venv should be
performed inside the container during the next build cycle.
