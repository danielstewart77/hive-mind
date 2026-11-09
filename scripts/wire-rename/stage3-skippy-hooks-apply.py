#!/usr/bin/env python3
"""Stage 3 of the comms wire rename, for Skippy's live (untracked) hook files.

These files take effect the moment they are written, and the running
skippy.service sets OWNER_TYPE/OWNER_REF on its spawns until it restarts on
the stage-3 code (which sets SURFACE/SURFACE_ID). Apply this together with
that restart, not before.

Renames: new_claude_sid -> new_conversation_id, claude_sid -> conversation_id
(marker-file and bg-task-file keys included, no fallback read),
owner_type -> surface, owner_ref -> surface_id, OWNER_TYPE -> SURFACE,
OWNER_REF -> SURFACE_ID. HIVE_SURFACE is untouched.

~/.claude/hooks/rotation_check.py is an installed copy of the repo's
staged-hooks/claude/rotation_check.py, so it is replaced by that file.

Dry run by default: prints a unified diff of every change. --apply writes
each file atomically, keeping its mode.
"""
from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
import tempfile
from pathlib import Path

HOME = Path.home()
REPO = HOME / "Storage" / "hive-edge-mind-skippy"
SKIPPY = REPO / "minds" / "skippy"

STAGED_CLAUDE_CHECK = REPO / "staged-hooks" / "claude" / "rotation_check.py"

# Installed copies replaced wholesale by their staged source.
COPIES = {
    HOME / ".claude" / "hooks" / "rotation_check.py": STAGED_CLAUDE_CHECK,
}

# Files rewritten in place by the substitutions below.
REWRITES = [
    HOME / ".claude" / "hooks" / "rotation_fire.py",
    HOME / ".claude" / "hooks" / "rotation_state.py",
    HOME / ".claude" / "hooks" / "bg_snapshot.py",
    HOME / ".claude" / "hooks" / "surface_inject.sh",
    SKIPPY / ".dsh" / "hooks" / "rotation_check.py",
    SKIPPY / ".dsh" / "hooks" / "rotation_fire.py",
    SKIPPY / ".dsh" / "hooks" / "rotation_state.py",
    SKIPPY / ".dsh" / "hooks" / "bg_snapshot.py",
    SKIPPY / ".dsh" / "hooks" / "surface_inject.sh",
    SKIPPY / ".codex" / "hooks" / "rotation_check.py",
    SKIPPY / ".codex" / "hooks" / "surface_inject.sh",
]

# Order matters: new_claude_sid before claude_sid.
SUBSTITUTIONS = [
    ("new_claude_sid", "new_conversation_id"),
    ("claude_sid", "conversation_id"),
    ("owner_type", "surface"),
    ("owner_ref", "surface_id"),
    ("OWNER_TYPE", "SURFACE"),
    ("OWNER_REF", "SURFACE_ID"),
]

OLD = re.compile(r"owner_type|owner_ref|claude_sid|OWNER_TYPE|OWNER_REF")


def rewrite(text: str) -> str:
    for old, new in SUBSTITUTIONS:
        text = text.replace(old, new)
    return text


def plan() -> list[tuple[Path, str, str]]:
    """(path, current text, new text) for every file that would change."""
    changes = []
    for target, source in COPIES.items():
        new = source.read_text()
        if OLD.search(new):
            sys.exit(f"refusing: {source} still carries an old name — "
                     "is the repo on the wire-drops-the-old-names branch?")
        changes.append((target, target.read_text(), new))
    for target in REWRITES:
        current = target.read_text()
        changes.append((target, current, rewrite(current)))
    for target, _, new in changes:
        left = OLD.findall(new)
        if left:
            sys.exit(f"refusing: {target} would still carry {sorted(set(left))}")
    return [c for c in changes if c[1] != c[2]]


def write_atomically(path: Path, text: str) -> None:
    mode = path.stat().st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args()

    changes = plan()
    for path, current, new in changes:
        sys.stdout.writelines(difflib.unified_diff(
            current.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile=f"a{path}", tofile=f"b{path}",
        ))
    if not args.apply:
        print(f"\n# dry run: {len(changes)} file(s) would change; rerun with --apply",
              file=sys.stderr)
        return 0
    for path, _, new in changes:
        write_atomically(path, new)
        print(f"wrote {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
