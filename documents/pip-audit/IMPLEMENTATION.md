# Implementation Plan: 1720154168930337855 - Add pip-audit Dependency Scanning to Dev Workflow

## Overview

Add automated dependency vulnerability scanning via `pip-audit` to the development workflow. This creates a `requirements-dev.txt` for dev-only tooling, a Python wrapper module (`core/dep_scan.py`) that runs pip-audit and parses its JSON output, a git pre-commit hook that blocks commits when critical vulnerabilities are found in changed requirements files, and developer-facing documentation for manual scanning and remediation.

## Technical Approach

- **Dev dependency file**: New `requirements-dev.txt` lists dev-only packages (pip-audit, pytest, etc.) separate from production `requirements.txt`. This follows standard Python convention and keeps the production image lean.
- **Wrapper module**: `core/dep_scan.py` provides a `run_pip_audit()` function that shells out to `pip-audit --format=json`, parses the structured output, and returns a typed result dataclass. This makes the scanning logic testable and reusable (pre-commit hook, future MCP tool, CI).
- **Pre-commit hook**: A bash script at `scripts/pre-commit-pip-audit.sh` that checks whether `requirements.txt` or `requirements-dev.txt` were modified in the staged changes. If so, it invokes `core/dep_scan.py` as a CLI (`python -m core.dep_scan`) and blocks the commit if critical/high vulnerabilities are found. Non-requirements commits pass through without scanning.
- **Hook installation**: The pre-commit hook is a standalone script. An install script (`scripts/install-hooks.sh`) symlinks or copies it into `.git/hooks/pre-commit`. This approach matches the existing pattern of the pre-push hook (`/.git/hooks/pre-push`) -- a simple bash script, no framework like pre-commit.
- **Documentation**: Remediation process documented in `documents/DEVELOPMENT.md` additions covering manual scan commands, interpreting results, and fixing vulnerabilities.
- **No CI pipeline**: The project has no GitHub Actions or CI pipeline (no `.github/` directory). The story AC says "pre-commit hook or CI pipeline" -- we use the pre-commit hook approach since the project already uses git hooks (pre-push for HITL).

## Reference Patterns

| Pattern | Source File | Usage |
|---------|------------|-------|
| Git hook (bash, gateway integration) | `.git/hooks/pre-push` | Bash hook pattern, exit codes for pass/fail |
| Subprocess execution (list args, no shell) | `agents/tool_creator.py:228-234` | `subprocess.run([...], capture_output=True, text=True)` |
| Test structure + pytest conventions | `tests/unit/test_audit.py` | Class-based test organization, pytest fixtures, assertions |
| Dev documentation format | `documents/DEVELOPMENT.md` | Section structure for developer guides |
| Security spec compliance | `specs/security.md` | "Do not install packages without telling the user what and why" |

## Models & Schemas

New dataclass in `core/dep_scan.py`:

```python
@dataclass
class VulnerabilityRecord:
    package: str       # e.g. "requests"
    version: str       # e.g. "2.25.0"
    vuln_id: str       # e.g. "PYSEC-2023-74"
    description: str   # brief vulnerability description
    fix_versions: list[str]  # versions that fix the issue, e.g. ["2.31.0"]

@dataclass
class ScanResult:
    success: bool              # True if pip-audit ran without errors
    vulnerabilities: list[VulnerabilityRecord]
    error: str | None          # error message if pip-audit failed to run
    raw_output: str            # raw JSON output from pip-audit

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
            + ", ".join(f"{v.package}=={v.version} ({v.vuln_id})" for v in self.vulnerabilities)
        )
```

## Implementation Steps

### Step 1: Create `requirements-dev.txt` with pip-audit

**Files:**
- Create: `requirements-dev.txt` -- dev-only Python dependencies (pip-audit, pytest)

**Test First (unit):** `tests/unit/test_dev_requirements.py`
- [ ] `test_requirements_dev_file_exists` -- asserts `requirements-dev.txt` exists at project root
- [ ] `test_requirements_dev_contains_pip_audit` -- asserts the file contains a line starting with `pip-audit`
- [ ] `test_requirements_dev_contains_pytest` -- asserts the file contains a line starting with `pytest`
- [ ] `test_requirements_dev_no_production_deps` -- asserts `requirements-dev.txt` does not duplicate core deps from `requirements.txt` (e.g. `fastapi`, `uvicorn`)

**Then Implement:**
- [ ] Create `requirements-dev.txt` at `/usr/src/app/requirements-dev.txt` containing:
  ```
  # Dev-only dependencies -- not installed in production containers
  pip-audit>=2.7.0
  pytest>=7.0
  ```
- [ ] The file should NOT be added to `.gitignore` (it is safe to track, unlike `config.yaml`)

**Verify:** `pytest tests/unit/test_dev_requirements.py -v`

---

### Step 2: Create `core/dep_scan.py` -- pip-audit wrapper with JSON parsing

**Files:**
- Create: `core/dep_scan.py` -- wrapper module that runs pip-audit and parses output

**Test First (unit):** `tests/unit/test_dep_scan.py`
- [ ] `test_parse_pip_audit_output_no_vulnerabilities` -- asserts parsing an empty vulnerabilities JSON returns `ScanResult(success=True, vulnerabilities=[])`
- [ ] `test_parse_pip_audit_output_with_vulnerabilities` -- asserts parsing JSON with vulnerability entries returns correct `VulnerabilityRecord` objects with all fields populated
- [ ] `test_parse_pip_audit_output_multiple_vulns_same_package` -- asserts multiple vulnerabilities for the same package are each captured as separate records
- [ ] `test_parse_pip_audit_invalid_json` -- asserts malformed JSON returns `ScanResult(success=False, error=...)` with appropriate error message
- [ ] `test_scan_result_has_vulnerabilities_property` -- asserts `has_vulnerabilities` returns True when list is non-empty, False when empty
- [ ] `test_scan_result_summary_no_vulns` -- asserts summary returns "No known vulnerabilities found." when clean
- [ ] `test_scan_result_summary_with_vulns` -- asserts summary includes count and package names
- [ ] `test_scan_result_summary_on_failure` -- asserts summary includes error message when `success=False`
- [ ] `test_run_pip_audit_calls_subprocess` -- mocks `subprocess.run`, asserts it is called with `[sys.executable, "-m", "pip_audit", "--format=json", "--output=-"]` and `shell=False`
- [ ] `test_run_pip_audit_returns_scan_result_on_success` -- mocks subprocess to return valid JSON, asserts `ScanResult` is correctly populated
- [ ] `test_run_pip_audit_returns_failure_on_nonzero_exit_with_no_json` -- mocks subprocess with returncode=1 and non-JSON stderr, asserts `ScanResult(success=False, error=...)`
- [ ] `test_run_pip_audit_handles_timeout` -- mocks subprocess to raise `subprocess.TimeoutExpired`, asserts `ScanResult(success=False, error="pip-audit timed out...")`
- [ ] `test_run_pip_audit_handles_file_not_found` -- mocks subprocess to raise `FileNotFoundError`, asserts `ScanResult(success=False, error="pip-audit is not installed...")`
- [ ] `test_run_pip_audit_with_requirements_file` -- mocks subprocess, asserts `-r requirements.txt` is appended when `requirements_file` arg is provided

**Then Implement:**
- [ ] Create `core/dep_scan.py` with:
  - `@dataclass class VulnerabilityRecord`: fields `package`, `version`, `vuln_id`, `description`, `fix_versions`
  - `@dataclass class ScanResult`: fields `success`, `vulnerabilities`, `error`, `raw_output`; properties `has_vulnerabilities`, `summary`
  - `def parse_pip_audit_output(raw_json: str) -> ScanResult`: parse pip-audit's `--format=json` output. The JSON structure is `{"dependencies": [{"name": "pkg", "version": "1.0", "vulns": [{"id": "...", "description": "...", "fix_versions": [...]}]}]}`. Iterate dependencies, extract those with non-empty `vulns` list.
  - `def run_pip_audit(requirements_file: str | None = None, timeout: int = 120) -> ScanResult`: execute `subprocess.run([sys.executable, "-m", "pip_audit", "--format=json", "--output=-"], ...)` following the pattern in `agents/tool_creator.py:228-234` (list args, `capture_output=True`, `text=True`, `timeout`). If `requirements_file` is provided, append `-r <path>`. Parse stdout with `parse_pip_audit_output()`. Handle `TimeoutExpired`, `FileNotFoundError`, and non-zero exit codes.
  - `if __name__ == "__main__":` CLI entry point that calls `run_pip_audit()`, prints the summary, and exits with code 1 if vulnerabilities found (for use by the pre-commit hook).

**Verify:** `pytest tests/unit/test_dep_scan.py -v`

---

### Step 3: Create pre-commit hook script

**Files:**
- Create: `scripts/pre-commit-pip-audit.sh` -- bash script that runs pip-audit on requirements changes

**Test First (unit):** `tests/unit/test_pre_commit_hook.py`
- [ ] `test_hook_script_exists` -- asserts `scripts/pre-commit-pip-audit.sh` exists and is executable
- [ ] `test_hook_script_checks_staged_requirements_files` -- reads the script content and asserts it uses `git diff --cached --name-only` to check for `requirements*.txt` changes
- [ ] `test_hook_script_invokes_dep_scan_module` -- reads the script content and asserts it calls `python -m core.dep_scan` (or the venv equivalent)
- [ ] `test_hook_script_exits_zero_on_no_requirements_changes` -- asserts the script has a conditional that skips scanning when no requirements files are staged

**Then Implement:**
- [ ] Create `scripts/pre-commit-pip-audit.sh`:
  ```bash
  #!/bin/bash
  # pip-audit pre-commit hook
  # Runs dependency vulnerability scanning when requirements files change.
  # Blocks commit if vulnerabilities are found.

  # Check if any requirements files are staged for commit
  STAGED_REQ=$(git diff --cached --name-only | grep -E '^requirements.*\.txt$')

  if [ -z "$STAGED_REQ" ]; then
      # No requirements files changed -- skip scan
      exit 0
  fi

  echo "[pip-audit] Requirements file(s) changed: $STAGED_REQ"
  echo "[pip-audit] Running dependency vulnerability scan..."

  # Determine Python executable (prefer venv)
  PYTHON="${VIRTUAL_ENV:-/opt/venv}/bin/python"
  if [ ! -f "$PYTHON" ]; then
      PYTHON="python3"
  fi

  # Run the scan via the dep_scan module
  $PYTHON -m core.dep_scan
  EXIT_CODE=$?

  if [ $EXIT_CODE -ne 0 ]; then
      echo ""
      echo "[pip-audit] Vulnerabilities found. Fix them before committing."
      echo "[pip-audit] Run '$PYTHON -m core.dep_scan' for details."
      echo "[pip-audit] To bypass (emergency only): git commit --no-verify"
      exit 1
  fi

  echo "[pip-audit] No known vulnerabilities found."
  exit 0
  ```
- [ ] Make the script executable: `chmod +x scripts/pre-commit-pip-audit.sh`

**Verify:** `pytest tests/unit/test_pre_commit_hook.py -v`

---

### Step 4: Create hook installation script

**Files:**
- Create: `scripts/install-hooks.sh` -- installs git hooks (pre-commit + preserves pre-push)

**Test First (unit):** `tests/unit/test_install_hooks.py`
- [ ] `test_install_script_exists` -- asserts `scripts/install-hooks.sh` exists and is executable
- [ ] `test_install_script_references_pre_commit_hook` -- reads the script content and asserts it references `pre-commit-pip-audit.sh`
- [ ] `test_install_script_preserves_existing_hooks` -- reads the script content and asserts it checks for existing pre-commit hook before overwriting (backs up or chains)

**Then Implement:**
- [ ] Create `scripts/install-hooks.sh`:
  ```bash
  #!/bin/bash
  # Install Hive Mind git hooks.
  # Safe to run multiple times -- backs up existing hooks.

  HOOKS_DIR="$(git rev-parse --show-toplevel)/.git/hooks"
  SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"

  # Install pre-commit hook
  TARGET="$HOOKS_DIR/pre-commit"
  if [ -f "$TARGET" ] && [ ! -L "$TARGET" ]; then
      echo "Backing up existing pre-commit hook to pre-commit.bak"
      cp "$TARGET" "$TARGET.bak"
  fi
  cp "$SCRIPTS_DIR/pre-commit-pip-audit.sh" "$TARGET"
  chmod +x "$TARGET"
  echo "Installed pre-commit hook (pip-audit)"

  echo "Done. Hooks installed."
  ```
- [ ] Make the script executable: `chmod +x scripts/install-hooks.sh`

**Verify:** `pytest tests/unit/test_install_hooks.py -v`

---

### Step 5: Add `__main__.py` CLI entry point for `core.dep_scan`

**Files:**
- Modify: `core/dep_scan.py` -- ensure the `if __name__ == "__main__"` block works correctly when invoked as `python -m core.dep_scan`
- Create: `core/__main__.py` is NOT needed -- the `if __name__ == "__main__"` in `dep_scan.py` combined with `python -m core.dep_scan` requires a `core/dep_scan/__main__.py` or direct invocation. Instead, add a simple `__main__` check in `dep_scan.py` that the pre-commit hook calls via `python -c "from core.dep_scan import run_pip_audit; ..."` or update the hook to call `python /usr/src/app/core/dep_scan.py` directly.

**Test First (unit):** `tests/unit/test_dep_scan_cli.py`
- [ ] `test_dep_scan_cli_exits_zero_on_clean_scan` -- mocks `run_pip_audit` to return clean result, asserts `main()` returns exit code 0
- [ ] `test_dep_scan_cli_exits_one_on_vulnerabilities` -- mocks `run_pip_audit` to return vulnerabilities, asserts `main()` returns exit code 1
- [ ] `test_dep_scan_cli_exits_one_on_scan_failure` -- mocks `run_pip_audit` to return `success=False`, asserts `main()` returns exit code 1
- [ ] `test_dep_scan_cli_prints_summary` -- mocks `run_pip_audit`, captures stdout, asserts summary text is printed

**Then Implement:**
- [ ] In `core/dep_scan.py`, add a `def main() -> int` function:
  ```python
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
  ```
- [ ] Update the `if __name__ == "__main__"` block to call `sys.exit(main())`
- [ ] Update the pre-commit hook in `scripts/pre-commit-pip-audit.sh` to invoke: `$PYTHON /usr/src/app/core/dep_scan.py` (direct script execution, avoiding `python -m` package path issues)

**Verify:** `pytest tests/unit/test_dep_scan_cli.py -v`

---

### Step 6: Document scan results and remediation process

**Files:**
- Modify: `documents/DEVELOPMENT.md` -- add "Dependency Scanning" section with pip-audit usage, interpreting results, and remediation steps

**Test First:** No tests required (pure documentation, no logic).

**Then Implement:**
- [ ] Add a new `## Dependency Scanning (pip-audit)` section to `documents/DEVELOPMENT.md` (after the "Testing" section), containing:

  ```markdown
  ## Dependency Scanning (pip-audit)

  The project uses `pip-audit` to scan Python dependencies for known vulnerabilities.
  A pre-commit hook automatically runs the scan when `requirements.txt` or
  `requirements-dev.txt` are modified.

  ### Setup

  Install dev dependencies (one-time):
  ```bash
  pip install -r requirements-dev.txt
  ```

  Install the pre-commit hook:
  ```bash
  bash scripts/install-hooks.sh
  ```

  ### Manual Scanning

  ```bash
  # Scan currently installed packages
  python core/dep_scan.py

  # Scan a specific requirements file
  pip-audit -r requirements.txt --format=json

  # Scan with verbose output
  pip-audit -r requirements.txt
  ```

  ### Interpreting Results

  pip-audit reports known vulnerabilities from the Python Packaging Advisory Database
  (PyPI) and the OSV database. Each finding includes:
  - **Package name and version** currently installed
  - **Vulnerability ID** (PYSEC-*, GHSA-*, CVE-*)
  - **Description** of the vulnerability
  - **Fix versions** (if available)

  ### Remediation Process

  1. **Review the finding** -- read the vulnerability description and assess impact
  2. **Check fix availability** -- if fix versions are listed, upgrade:
     ```bash
     pip install package-name==<fix-version>
     ```
  3. **Update requirements.txt** -- pin the fixed version
  4. **Re-run the scan** to confirm the fix:
     ```bash
     python core/dep_scan.py
     ```
  5. **If no fix exists** -- evaluate whether the vulnerability applies to this
     project's usage of the package. Document the decision and add a comment
     in requirements.txt if the risk is accepted.

  ### Bypassing the Hook (Emergency)

  If you must commit despite a vulnerability (e.g., no fix available yet):
  ```bash
  git commit --no-verify -m "reason for bypass"
  ```
  Document the bypass reason in the commit message.
  ```

**Verify:** Read the updated file to confirm formatting is correct.

---

### Step 7: Run initial scan and document baseline results

**Files:**
- Create: `documents/pip-audit/SCAN-RESULTS.md` -- baseline scan results from the initial run

**Test First:** No tests required (documentation artifact).

**Then Implement:**
- [ ] Install pip-audit in the dev environment: `/opt/venv/bin/pip install pip-audit`
- [ ] Run the initial scan: `/opt/venv/bin/python /usr/src/app/core/dep_scan.py`
- [ ] Capture the output and create `documents/pip-audit/SCAN-RESULTS.md` with:
  - Date of scan
  - Python version and pip-audit version
  - Full scan output (clean or with findings)
  - Any remediation actions taken or risk acceptances documented
  - This satisfies AC: "Scan results are documented"

**Verify:** File exists with scan results.

---

### Step 8: End-to-end integration test

**Files:**
- Create: `tests/integration/test_pip_audit_integration.py` -- end-to-end test of the scanning workflow

**Test First (integration):** `tests/integration/test_pip_audit_integration.py`
- [ ] `test_dep_scan_module_imports_cleanly` -- asserts `from core.dep_scan import run_pip_audit, ScanResult, VulnerabilityRecord` succeeds without errors
- [ ] `test_dep_scan_parse_round_trip` -- constructs pip-audit JSON output, parses it, asserts all fields survive the round trip
- [ ] `test_pre_commit_hook_script_is_valid_bash` -- runs `bash -n scripts/pre-commit-pip-audit.sh` to validate syntax
- [ ] `test_install_hooks_script_is_valid_bash` -- runs `bash -n scripts/install-hooks.sh` to validate syntax
- [ ] `test_dep_scan_main_returns_int` -- calls `main()` with mocked subprocess, asserts return type is `int`

**Then Implement:** Tests only -- no new implementation code in this step.

**Verify:** `pytest tests/integration/test_pip_audit_integration.py -v`

---

## Integration Checklist

- [ ] No new routes needed in `server.py` (this is a dev workflow tool, not a runtime feature)
- [ ] No new MCP tools needed (pip-audit is a local dev tool, not a Claude-facing capability)
- [ ] No config additions needed in `config.py` / `config.yaml`
- [ ] `requirements-dev.txt` created with `pip-audit>=2.7.0` and `pytest>=7.0`
- [ ] `core/dep_scan.py` created with `run_pip_audit()`, `parse_pip_audit_output()`, `main()`
- [ ] `scripts/pre-commit-pip-audit.sh` created and executable
- [ ] `scripts/install-hooks.sh` created and executable
- [ ] `documents/DEVELOPMENT.md` updated with Dependency Scanning section
- [ ] `documents/pip-audit/SCAN-RESULTS.md` created with baseline results
- [ ] No secrets involved (pip-audit queries public vulnerability databases)

## Build Verification

- [ ] `pytest tests/ -v` passes
- [ ] `mypy core/dep_scan.py --ignore-missing-imports` passes
- [ ] `ruff check core/dep_scan.py` passes
- [ ] All 4 ACs addressed:
  1. pip-audit is added to requirements-dev.txt (Step 1)
  2. pip-audit is integrated into the pre-commit hook (Steps 3, 4, 5)
  3. Scan results are documented (Step 7)
  4. Remediation process is documented for developers (Step 6)
