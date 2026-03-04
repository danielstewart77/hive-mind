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

# Run the scan via dep_scan.py directly
$PYTHON /usr/src/app/core/dep_scan.py
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo ""
    echo "[pip-audit] Vulnerabilities found. Fix them before committing."
    echo "[pip-audit] Run '$PYTHON /usr/src/app/core/dep_scan.py' for details."
    echo "[pip-audit] To bypass (emergency only): git commit --no-verify"
    exit 1
fi

echo "[pip-audit] No known vulnerabilities found."
exit 0
