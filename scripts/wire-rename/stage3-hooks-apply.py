#!/usr/bin/env python3
"""Stage 3 of the comms wire rename, for the gitignored live hooks.

Renames the old session vocabulary in every untracked file under
``<repo>/minds/**/hooks/``: ``claude_sid`` -> ``conversation_id``,
``owner_type`` -> ``surface``, ``owner_ref`` -> ``surface_id`` (identifiers,
dict keys and marker-file keys alike), and the environment variables
``OWNER_TYPE`` -> ``SURFACE``, ``OWNER_REF`` -> ``SURFACE_ID``,
``HIVEMIND_OWNER_TYPE`` -> ``HIVEMIND_SURFACE``, ``HIVEMIND_OWNER_REF`` ->
``HIVEMIND_SURFACE_ID``. ``HIVE_SURFACE`` (the label) is untouched.

Usage:
    stage3-hooks-apply.py <repo>            # dry run: print the diff
    stage3-hooks-apply.py <repo> --apply    # write the files

Exit status is non-zero when old names are present but nothing would be (or
was) rewritten, when any old name survives the rewrite, or when a rewritten
Python file no longer compiles (nothing is written in that case).
"""

from __future__ import annotations

import difflib
import re
import subprocess
import sys
from pathlib import Path

RULES = [
    (re.compile(r"new_claude_sid"), "new_conversation_id"),
    (re.compile(r"\bclaude_sid\b"), "conversation_id"),
    (re.compile(r"\bprevious_owner_type\b"), "previous_surface"),
    (re.compile(r"\bprevious_owner_ref\b"), "previous_surface_id"),
    # Substring on purpose: catches HIVEMIND_OWNER_TYPE as well.
    (re.compile(r"OWNER_TYPE"), "SURFACE"),
    (re.compile(r"OWNER_REF"), "SURFACE_ID"),
    (re.compile(r"\bowner_type\b"), "surface"),
    (re.compile(r"\bowner_ref\b"), "surface_id"),
]
OLD = re.compile(r"owner_type|owner_ref|claude_sid|OWNER_TYPE|OWNER_REF")


def hook_files(repo: Path) -> list[Path]:
    tracked = set(
        subprocess.run(
            ["git", "-C", str(repo), "ls-files", "minds"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
    )
    found = []
    for path in sorted((repo / "minds").glob("**/hooks/*")):
        rel = path.relative_to(repo).as_posix()
        if (not path.is_file() or "__pycache__" in path.parts
                or "plugins" in path.parts or "projects" in path.parts
                or rel in tracked):
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if OLD.search(text):
            found.append(path)
    return found


def rename(text: str) -> str:
    for pattern, new in RULES:
        text = pattern.sub(new, text)
    return text


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--apply"]
    apply = "--apply" in argv[1:]
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    repo = Path(args[0]).resolve()
    files = hook_files(repo)
    present = len(files)

    rewritten: dict[Path, str] = {}
    broken: list[str] = []
    for path in files:
        old = path.read_text()
        new = rename(old)
        if new == old:
            continue
        if path.suffix == ".py":
            try:
                compile(new, str(path), "exec")
            except SyntaxError as exc:
                broken.append(f"{path}: {exc}")
        rewritten[path] = new
        sys.stdout.writelines(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile=f"a/{path.relative_to(repo)}", tofile=f"b/{path.relative_to(repo)}",
        ))

    survivors = [
        f"{path.relative_to(repo)}:{n}: {line.strip()}"
        for path, text in rewritten.items()
        for n, line in enumerate(text.splitlines(), 1) if OLD.search(line)
    ]
    verb = "rewrote" if apply else "would rewrite"
    print(f"# {verb} {len(rewritten)} of {present} hook files carrying old names",
          file=sys.stderr)

    if broken:
        print("# refusing: these no longer compile after the rename:", file=sys.stderr)
        print("\n".join(broken), file=sys.stderr)
        return 1
    if survivors:
        print(f"# refusing: {len(survivors)} old names survive the rename:", file=sys.stderr)
        print("\n".join(survivors), file=sys.stderr)
        return 1
    if present and not rewritten:
        print(f"# FAIL: {present} files carry old names and none was rewritten",
              file=sys.stderr)
        return 1

    if apply:
        for path, text in rewritten.items():
            path.write_text(text)
        remaining = hook_files(repo)
        if remaining:
            print(f"# FAIL: {len(remaining)} files still carry old names after writing:",
                  file=sys.stderr)
            print("\n".join(str(p) for p in remaining), file=sys.stderr)
            return 1
    if not present:
        print("# nothing to do: no hook file carries an old name", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
