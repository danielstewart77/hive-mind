#!/usr/bin/env python3
"""Refuse to let a credential reach a commit.

The corpus this repo captures is lossless by contract, so live credentials
sit in ``training_turns.db`` permanently and land in any session that reads
it. Everything downstream of that is judgment, and judgment is exactly what
failed on 2026-07-30: real values were pasted into a test fixture and only
GitHub's pre-receive scanner stopped the push — on a public repo, and only
because the one credential it happened to pattern-match was sitting next to
two it does not.

This is the control that does not depend on remembering. It scans the added
lines of a staged diff with the same detector the export path uses, so there
is one ruleset and no second opinion to drift.

    secret_guard.py scan-staged
    secret_guard.py scan-path core/ tools/
    secret_guard.py install --hooks-dir ~/.git-hooks

Only added lines are scanned: a deletion that removes a secret must not be
blocked, or the fix for a leak is unpushable. A line carrying the marker
``secret-guard: allow`` is skipped, which is how a fixture documents that
its token is invented.

``git commit --no-verify`` still bypasses this, by git's design. That is
acceptable — the goal is to make the accident impossible, not the deliberate
act.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.training_redaction import find_secrets  # noqa: E402

ALLOW_MARKER = "secret-guard: allow"

# A credential detector needs fixtures shaped like credentials, so the test
# suite is unavoidably full of token-shaped strings. Exempting them by path
# is exactly the wrong fix — the 2026-07-30 incident *was* a test file. The
# exemption is therefore a property of the value: a fixture announces that
# it is invented inside the string itself, where no real credential can
# accidentally agree.
FAKE_TOKENS = ("INVENTED", "EXAMPLE", "FIXTURE", "NOTAREAL", "PLACEHOLDER")


def _announces_itself_as_fake(text: str) -> bool:
    upper = text.upper()
    return any(marker in upper for marker in FAKE_TOKENS)

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _git(args: list[str], cwd: str | None = None) -> str:
    """Run git and return stdout, or raise with git's own message."""
    proc = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def added_lines(diff: str) -> list[tuple[str, int, str]]:
    """Extract ``(path, line_number, text)`` for every added line in a diff.

    Deletions and context are dropped. Binary files carry no ``+`` lines and
    so fall out naturally.
    """
    out: list[tuple[str, int, str]] = []
    path = ""
    lineno = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            path = "" if target == "/dev/null" else target[2:] if target.startswith("b/") else target
            continue
        if raw.startswith("@@"):
            match = _HUNK_RE.match(raw)
            if match:
                lineno = int(match.group(1))
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            if path:
                out.append((path, lineno, raw[1:]))
            lineno += 1
        elif not raw.startswith("-") and not raw.startswith("\\"):
            lineno += 1
    return out


def scan_lines(lines: list[tuple[str, int, str]]) -> list[dict]:
    """Return one finding per credential, previewed and never quoted."""
    findings: list[dict] = []
    seen: set[tuple[str, int, str]] = set()
    for path, lineno, text in lines:
        if ALLOW_MARKER in text or _announces_itself_as_fake(text):
            continue
        for hit in find_secrets(text):
            # Rules overlap by design — a token in an assignment matches both
            # the vendor pattern and the assignment pattern. That is one
            # credential to fix, so it is one finding to report.
            key = (path, lineno, hit.preview)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                {
                    "path": path,
                    "line": lineno,
                    "rule": hit.rule,
                    "preview": hit.preview,
                }
            )
    return findings


def scan_staged(cwd: str | None = None) -> list[dict]:
    """Scan what ``git commit`` is about to record."""
    diff = _git(["diff", "--cached", "--unified=0", "--no-color"], cwd=cwd)
    return scan_lines(added_lines(diff))


SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}
)


def _walk(root: Path):
    """Every file under ``root``, minus the trees nothing is committed from.

    Without this an audit of a checkout spends its time inside a virtualenv
    and a node_modules, which are two orders of magnitude larger than the
    source and are not what any commit contains.
    """
    for path in sorted(root.iterdir()):
        if path.name in SKIP_DIRS:
            continue
        if path.is_symlink():
            continue
        if path.is_dir():
            yield from _walk(path)
        elif path.is_file():
            yield path


def _candidates(root: Path):
    """Tracked files under ``root`` when it is in a repo, else every file.

    An audit answers "what could reach a commit", so a gitignored `.env` or
    a mind's private directory is out of scope by definition — and scanning
    them turns the report into thousands of findings about files git will
    never see.
    """
    if not root.is_dir():
        return [root]
    try:
        listing = _git(["ls-files", "-z", "--", "."], cwd=str(root))
    except (RuntimeError, OSError):
        return _walk(root)
    return [root / name for name in listing.split("\0") if name]


def scan_paths(paths: list[str]) -> list[dict]:
    """Scan working-tree files, for auditing a checkout after the fact."""
    lines: list[tuple[str, int, str]] = []
    for entry in paths:
        root = Path(entry)
        candidates = _candidates(root)
        for candidate in candidates:
            try:
                text = candidate.read_text(errors="strict")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                lines.append((str(candidate), number, line))
    return scan_lines(lines)


# The repo-local hook is addressed through --git-dir, never --git-path.
# ``git rev-parse --git-path hooks/pre-commit`` is core.hooksPath-aware and
# so resolves to the installed hook itself, which re-executes and hangs the
# commit in a fork loop rather than failing.
_CHAIN = """local_hook="$(git rev-parse --git-dir)/hooks/{name}"
if [ -x "$local_hook" ]; then
    exec "$local_hook" "$@"
fi
"""

PRE_COMMIT_HOOK = (
    """#!/bin/sh
# Installed by tools/stateless/secret_guard. Blocks a commit whose added
# lines contain a credential, then chains to this repo's own hook if it has
# one — core.hooksPath replaces .git/hooks rather than adding to it.
set -e

if ! "{python}" "{guard}" scan-staged --quiet; then
    exit 1
fi

"""
    + _CHAIN.format(name="pre-commit")
)

CHAIN_HOOK = (
    """#!/bin/sh
# Installed by tools/stateless/secret_guard. core.hooksPath replaces
# .git/hooks wholesale, so every other hook this host relies on has to be
# chained through explicitly or it silently stops running.
set -e
"""
    + _CHAIN
)

CHAINED_HOOKS = (
    "pre-push",
    "commit-msg",
    "prepare-commit-msg",
    "post-commit",
    "post-checkout",
    "post-merge",
)


def install(hooks_dir: Path) -> dict:
    """Write the hook set and point ``core.hooksPath`` at it globally.

    Every repo on the host is covered at once, including ones cloned later,
    which is the whole point — a per-repo install protects only the repos
    somebody remembered to install it in.
    """
    hooks_dir = hooks_dir.expanduser()
    hooks_dir.mkdir(parents=True, exist_ok=True)

    guard = Path(__file__).resolve()
    written = []

    pre_commit = hooks_dir / "pre-commit"
    pre_commit.write_text(
        PRE_COMMIT_HOOK.replace("{python}", sys.executable).replace("{guard}", str(guard))
    )
    pre_commit.chmod(0o755)
    written.append(str(pre_commit))

    for name in CHAINED_HOOKS:
        path = hooks_dir / name
        path.write_text(CHAIN_HOOK.format(name=name))
        path.chmod(0o755)
        written.append(str(path))

    _git(["config", "--global", "core.hooksPath", str(hooks_dir)])
    return {"hooks_dir": str(hooks_dir), "written": written, "guard": str(guard)}


def _report(findings: list[dict], quiet: bool) -> int:
    if quiet:
        if findings:
            sys.stderr.write(
                "secret-guard: refusing the commit — "
                f"{len(findings)} credential(s) in added lines.\n"
            )
            for hit in findings:
                sys.stderr.write(
                    f"  {hit['path']}:{hit['line']}  {hit['rule']}  {hit['preview']}\n"
                )
            sys.stderr.write(
                "\nThe value itself is never printed. Replace it with an invented "
                f"string, or mark the line '{ALLOW_MARKER}' if it is already fake.\n"
                "Deliberate override: git commit --no-verify\n"
            )
        return 1 if findings else 0

    json.dump({"ok": not findings, "count": len(findings), "findings": findings}, sys.stdout)
    sys.stdout.write("\n")
    return 1 if findings else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    staged = sub.add_parser("scan-staged", help="scan the staged diff")
    staged.add_argument("--quiet", action="store_true", help="human output, for hook use")

    paths = sub.add_parser("scan-path", help="scan files or directories")
    paths.add_argument("paths", nargs="+")
    paths.add_argument("--quiet", action="store_true")

    installer = sub.add_parser("install", help="install the global hook set")
    installer.add_argument("--hooks-dir", default=os.path.expanduser("~/.git-hooks"))

    args = parser.parse_args(argv)

    if args.command == "install":
        json.dump(install(Path(args.hooks_dir)), sys.stdout)
        sys.stdout.write("\n")
        return 0

    findings = scan_staged() if args.command == "scan-staged" else scan_paths(args.paths)
    return _report(findings, args.quiet)


if __name__ == "__main__":
    sys.exit(main())
