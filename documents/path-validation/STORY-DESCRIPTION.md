# [Security MEDIUM-4] No Path Validation on Skill documents_path

**Card ID:** 1720154166069822499

## Description

A path traversal vulnerability exists in three skill agent files where `documents_path` is passed directly to `subprocess.run()` without validation. Malicious or misconfigured paths (such as `/etc/shadow`, `../../.env`, or symlinks to sensitive files) could be processed, potentially exposing or modifying protected files.

**Affected Files:**
- `agents/skill_planning_genius.py:22`
- `agents/skill_code_genius.py:20`
- `agents/skill_code_review_genius.py:20`

**CWE Reference:** CWE-22 (Improper Limitation of a Pathname to a Restricted Directory — Path Traversal)

## Acceptance Criteria

- [ ] Path validation added to all three skill agent files
- [ ] `os.path.realpath()` used to resolve symlinks and canonicalize paths
- [ ] Validated that `documents_path` is within expected `documents/` directory
- [ ] Paths outside the allowed directory are rejected with clear error messages
- [ ] Unit tests cover both valid paths and path traversal attack vectors (e.g., `../`, `../../.env`, absolute paths to sensitive files)
- [ ] Integration test confirms skills reject malicious paths gracefully

## Tasks

- [ ] Review skill_planning_genius.py for path handling
- [ ] Review skill_code_genius.py for path handling
- [ ] Review skill_code_review_genius.py for path handling
- [ ] Implement validation utility function in core/ for reusable path checking
- [ ] Update all three skill files to call the validation function before subprocess.run()
- [ ] Add unit tests for path validation edge cases
- [ ] Test symlink resolution and traversal prevention
- [ ] Code review and merge
