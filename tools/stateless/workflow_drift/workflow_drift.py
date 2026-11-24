#!/usr/bin/env python3
"""Report which generated workflow skills have drifted from how they were made.

    workflow_drift.py [--root DIR] [--skills DIR]

Each generated skill lives in `<skills>/<skill>/`, and the untouched copy it
was generated as lives in `<skills>/.as-generated/<skill>/`. That copy holds
the same files the skill was generated with plus one record file,
`.generated.json`, which is not part of the skill:

    {"workflow": "<workflow name>",
     "document": "<path of the stage document>",
     "document_version": "<sha256 hex of that file's bytes>",
     "top_document": "<path of the top document>",
     "top_version": "<sha256 hex of its bytes>"}

All five keys are required. Both paths are relative to the checkout root
(`--root`, by default the checkout this script lives in), never absolute and
never containing `..`. A document's version is the sha256 of its bytes, so a
mind can compute it with `sha256sum <document>`.

One line per finding on stdout, sorted by skill name, nothing at all when
there is nothing to report. Exit 0 when nothing was reported, 1 when
anything was.

    <skill>: tuned     a file of the skill differs from its as-generated
                       copy: changed, added or removed (every file counts,
                       the record file apart)
    <skill>: stale     the stage document is not the version the skill was
                       generated from; a second line says the same of the
                       top document, so a changed top document names every
                       skill generated under it
    <skill>: problem   a skill with no as-generated copy, a copy whose
                       skill is gone, a record that is missing, unreadable,
                       not an object, lacking a key or naming an unsafe
                       path, or a document the record names that no longer
                       exists

Names starting with a dot are not skills. The script only reads: it writes
nothing, touches no network and needs nothing but the standard library.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

#: The folder, inside the skills folder, that holds the as-generated copies.
COPIES = ".as-generated"
#: The record file inside each copy. It is not part of the skill.
RECORD = ".generated.json"
_KEYS = ("workflow", "document", "document_version",
         "top_document", "top_version")

_ROOT = Path(__file__).resolve().parents[3]


def _files(folder: Path, skip_record: bool) -> dict[str, bytes | None]:
    """Every file under `folder` as relative path -> bytes (None when the
    file cannot be read, which still differs from a readable one)."""
    found: dict[str, bytes | None] = {}
    for here, dirs, names in os.walk(folder):
        for name in names:
            path = Path(here) / name
            rel = path.relative_to(folder).as_posix()
            if skip_record and rel == RECORD:
                continue
            try:
                found[rel] = path.read_bytes()
            except OSError:
                found[rel] = None
    return found


def _unsafe(path: object) -> bool:
    if not isinstance(path, str) or not path:
        return True
    if PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute():
        return True
    return ".." in path.replace("\\", "/").split("/")


def _version(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError):
        return None


def _record(copy: Path) -> tuple[dict | None, str | None]:
    """The copy's record, or the reason there is none to use."""
    try:
        data = json.loads((copy / RECORD).read_bytes().decode("utf-8"))
    except FileNotFoundError:
        return None, "record missing"
    except (OSError, ValueError, RecursionError):
        return None, "record unreadable"
    if not isinstance(data, dict):
        return None, "record is not an object"
    missing = [k for k in _KEYS if k not in data]
    if missing:
        return None, "record lacks " + ", ".join(missing)
    for key in ("document", "top_document"):
        if _unsafe(data[key]):
            return None, f"record {key} is not a path inside the checkout"
    for key in ("document_version", "top_version"):
        if not isinstance(data[key], str):
            return None, f"record {key} is not a string"
    return data, None


def _check(skill: str, skills: Path, root: Path) -> list[str]:
    out: list[str] = []
    live, copy = skills / skill, skills / COPIES / skill
    if not live.is_dir():
        return [f"{skill}: problem: as-generated copy has no skill"]
    if not copy.is_dir():
        return [f"{skill}: problem: no as-generated copy"]
    if _files(live, False) != _files(copy, True):
        out.append(f"{skill}: tuned")
    record, why = _record(copy)
    if record is None:
        out.append(f"{skill}: problem: {why}")
        return out
    for label, doc, want in (
            ("stage document", record["document"], record["document_version"]),
            ("top document", record["top_document"], record["top_version"])):
        have = _version(root / doc)
        if have is None:
            out.append(f"{skill}: problem: {label} {doc} no longer exists")
        elif have != want.strip().lower():
            out.append(f"{skill}: stale: {label} {doc} has changed")
    return out


def findings(root: Path, skills: Path) -> list[str]:
    names: set[str] = set()
    for folder in (skills, skills / COPIES):
        if folder.is_dir():
            names.update(p.name for p in folder.iterdir()
                         if p.is_dir() and not p.name.startswith("."))
    lines: list[str] = []
    for name in sorted(names):
        lines.extend(_check(name, skills, root))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", type=Path, default=_ROOT,
                        help="the hive_mind checkout the documents live in")
    parser.add_argument("--skills", type=Path, default=None,
                        help="the generated skills folder "
                             "(default: <root>/install/workflow-skills)")
    args = parser.parse_args(argv)
    skills = args.skills or args.root / "install" / "workflow-skills"
    lines = findings(args.root, skills)
    for line in lines:
        print(line)
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main())
