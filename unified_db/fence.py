"""After the switch, a write to a retired store fails instead of succeeding.

The cutover leaves every SQLite file on disk, frozen, because that is the way
back. What it does not leave is any protection: the data directory is
bind-mounted read-write with no `:ro`, the files are 0644 and the containers
run as root, so any code path still pointing at SQLite writes successfully
and silently. On the proxy cutover the loader's drift check is what caught a
stray writer — after this switch nothing is running that would notice, and
the hive would carry two divergent copies of its conversations with both
looking healthy.

Three writers in comms bypass the access layer and so are not caught by
anything the access layer refuses: `comms/broker.py`, `comms/sessions.py`,
and `comms/inter_mind_api/server.py`, which opens its own connection from its
own module-level path rather than the gateway's. A configuration change that
repoints comms does not necessarily repoint all three. This catches whatever
the configuration misses.

## Why a directory and not permissions

Moving the file aside is not a fence. `sqlite3.connect` on a path with no
file there **creates one** — that is exactly how the quiet second copies in
`hive_mind/data/` came to exist — so a stray writer handed a missing path
mints an empty store, answers every read with nothing, and reports success.
Permissions are not a fence either: the containers run as root, and root
ignores the mode bits.

What does hold is a *directory* standing where the file was. Both `sqlite3`
and `aiosqlite` answer `OperationalError: unable to open database file`,
which is loud, immediate and impossible to mistake for an empty store. It
needs no root, no mount change and no daemon.

## Why this is a command and not configuration

A write fence made of file permissions is not "a configuration change and a
restart", so it argues with the revert requirement — and that argument is
why this is its own module. DECIDED 2026-11-07 by Daniel: the revert is a
short documented command sequence rather than configuration alone. So the
fence records what it moved, and `--revert` puts every file back exactly
where it was. The sequence is printed when the fence goes up, written into
the journal, and left inside each placeholder directory, because the one
moment an operator needs the way back is the moment they cannot read the
terminal that printed it.

    python -m unified_db.fence --base /home/daniel/Storage
    python -m unified_db.fence --revert
    python -m unified_db.fence --inspect
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import schema_map

#: Where the fence records what it moved. In the working directory, not a
#: temporary one, for the same reason the postflight probe's journal is: a
#: file that gets swept away is no use to the operator who needs to undo it.
JOURNAL_VAR = "FENCE_JOURNAL"

#: What goes inside each placeholder directory. The journal is the machine's
#: record; this is the one a person finds while looking at the wrong thing.
MARKER_NAME = "RETIRED.txt"


def journal_path() -> Path:
    return Path(os.environ.get(JOURNAL_VAR) or Path.cwd() / "fence.json")


@dataclass
class Fenced:
    """One store, where it went, and what it was when it got there."""

    source: str
    path: str
    moved_to: str
    size: int
    mtime: float


@dataclass
class Absent:
    """A store named in the map that is not on this host."""

    source: str
    path: str


@dataclass
class Report:
    fenced: list[Fenced] = field(default_factory=list)
    absent: list[Absent] = field(default_factory=list)
    #: Per file, what is wrong with it. Empty is the answer the proof wants.
    moved: list[str] = field(default_factory=list)
    at: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.moved


def _marker_text(entry: Fenced) -> str:
    return (
        "This directory is not a database. It is standing in the way of one.\n"
        "\n"
        f"The store that was here has been moved to:\n  {entry.moved_to}\n"
        "\n"
        "It was retired by the Postgres cutover and nothing should be\n"
        "writing to it. A directory is here rather than nothing because\n"
        "sqlite creates a database on a path with no file, which would mint\n"
        "an empty second store and report success.\n"
        "\n"
        "To put it back:\n"
        f"  rm -rf {entry.path!r}\n"
        f"  mv {entry.moved_to!r} {entry.path!r}\n"
        "\n"
        "Or, for every store at once, from the hive_mind checkout:\n"
        "  python -m unified_db.fence --revert\n"
    )


def store_paths(base: str, sources: set[str] | None = None) -> dict[str, str]:
    """Every store this fence covers, by name.

    The sources the loader read, plus the quiet second copies. Those copies
    are the reason this module exists in the shape it does: they are real
    SQLite files, nothing in the map names them, and a revert performed from
    the host checkout rather than the container finds them and comes up
    healthy with zero conversations in it.
    """
    paths = dict(schema_map.source_paths(base))
    paths.update(schema_map.quiet_copy_paths(base))
    if sources is not None:
        # `--only lucent` means lucent's store *and* lucent's quiet copy.
        # Matching on the whole key would fence the live file and leave the
        # copy open, which is the exact pair this is here to close.
        paths = {name: path for name, path in paths.items()
                 if name.rsplit(":", 1)[-1] in sources}
    return paths


def raise_fence(base: str, *, sources: set[str] | None = None,
                journal: Path | None = None) -> Report:
    """Move every retired store aside and stand a directory in its place.

    Idempotent in the only direction that matters: a path that is already a
    directory is reported absent rather than moved, so a second run cannot
    bury a placeholder inside a placeholder.
    """
    report = Report(at=time.time())
    for source, path_str in sorted(store_paths(base, sources).items()):
        path = Path(path_str)
        if path.is_dir():
            report.absent.append(Absent(source, str(path)))
            continue
        if not path.exists():
            report.absent.append(Absent(source, str(path)))
            continue
        stat = path.stat()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(report.at))
        moved_to = path.with_name(f"{path.name}.retired-{stamp}")
        path.rename(moved_to)
        # The sidecars go with it. A `-wal` left beside a placeholder is a
        # file whose own name says a database is open, which is the kind of
        # thing somebody reads as evidence months later.
        for suffix in ("-wal", "-shm"):
            sidecar = path.with_name(path.name + suffix)
            if sidecar.exists():
                sidecar.rename(moved_to.with_name(moved_to.name + suffix))
        entry = Fenced(source=source, path=str(path), moved_to=str(moved_to),
                       size=stat.st_size, mtime=stat.st_mtime)
        path.mkdir()
        (path / MARKER_NAME).write_text(_marker_text(entry))
        report.fenced.append(entry)
    write_journal(report, journal)
    return report


def lower_fence(journal: Path | None = None) -> Report:
    """Put every store back where it was. The revert.

    Refuses a placeholder holding anything but the marker. A directory that
    has grown contents is something else now, and the one thing worse than a
    fence that will not come down is a revert that deletes a directory
    somebody has been using.
    """
    report = read_journal(journal)
    restored = Report(at=time.time())
    for entry in report.fenced:
        path, moved_to = Path(entry.path), Path(entry.moved_to)
        if not moved_to.exists():
            restored.moved.append(
                f"{entry.source}: {moved_to} is not there, so there is "
                "nothing to put back")
            continue
        if path.is_dir():
            contents = {p.name for p in path.iterdir()}
            if contents - {MARKER_NAME}:
                restored.moved.append(
                    f"{entry.source}: {path} is a directory holding "
                    f"{sorted(contents - {MARKER_NAME})}, which this did not "
                    "put there. Left alone.")
                continue
            shutil.rmtree(path)
        elif path.exists():
            restored.moved.append(
                f"{entry.source}: {path} is a file again already, and it is "
                "not the one that was moved. Left alone.")
            continue
        moved_to.rename(path)
        for suffix in ("-wal", "-shm"):
            sidecar = moved_to.with_name(moved_to.name + suffix)
            if sidecar.exists():
                sidecar.rename(path.with_name(path.name + suffix))
        restored.fenced.append(entry)
    if restored.ok:
        jpath = journal or journal_path()
        if jpath.exists():
            jpath.unlink()
    return restored


def inspect(journal: Path | None = None) -> Report:
    """Whether every retired store is exactly where the fence left it.

    This is the half of the cutover's proof that nothing asserted. "While the
    old files stay frozen" was a sentence in the plan and in no check: the
    postflight never looked at the files at all, no path, no size, no
    modification time. So a stray writer could append to a retired store for
    a month and every proof would stay green.
    """
    recorded = read_journal(journal)
    out = Report(at=time.time(), absent=recorded.absent)
    for entry in recorded.fenced:
        path, moved_to = Path(entry.path), Path(entry.moved_to)
        if not moved_to.exists():
            out.moved.append(f"{entry.source}: {moved_to} is gone")
            continue
        stat = moved_to.stat()
        if stat.st_size != entry.size:
            out.moved.append(
                f"{entry.source}: {moved_to} was {entry.size} bytes and is "
                f"now {stat.st_size} — something wrote to a retired store")
            continue
        if stat.st_mtime != entry.mtime:
            out.moved.append(
                f"{entry.source}: {moved_to} has the same size and a "
                f"different modification time — something opened it for "
                "writing")
            continue
        if not path.is_dir():
            out.moved.append(
                f"{entry.source}: {path} is not a directory any more, so the "
                "fence is down and a writer there would be creating a second "
                "store")
            continue
        out.fenced.append(entry)
    return out


def revert_commands(journal: Path | None = None) -> list[str]:
    """The documented way back, as the lines to type.

    Printed rather than described. The revert is a command sequence by
    decision, which only works if the sequence is written down somewhere an
    operator can read it under pressure.
    """
    recorded = read_journal(journal)
    if not recorded.fenced:
        return []
    lines = ["python -m unified_db.fence --revert", "", "or, by hand:"]
    for entry in recorded.fenced:
        lines.append(f"  rm -rf {entry.path!r} && "
                     f"mv {entry.moved_to!r} {entry.path!r}")
    return lines


def write_journal(report: Report, journal: Path | None = None) -> Path:
    path = journal or journal_path()
    path.write_text(json.dumps({
        "at": report.at,
        "fenced": [asdict(e) for e in report.fenced],
        "absent": [asdict(e) for e in report.absent],
        "revert": revert_commands_for(report),
    }, indent=2) + "\n")
    return path


def revert_commands_for(report: Report) -> list[str]:
    """The same sequence, for a report in hand rather than one on disk."""
    return [f"rm -rf {e.path!r} && mv {e.moved_to!r} {e.path!r}"
            for e in report.fenced]


def read_journal(journal: Path | None = None) -> Report:
    path = journal or journal_path()
    if not path.exists():
        raise FileNotFoundError(
            f"no fence journal at {path}. Either the fence was never raised, "
            f"or it was raised somewhere else — set {JOURNAL_VAR} to where "
            "it is.")
    raw = json.loads(path.read_text())
    return Report(
        at=float(raw.get("at") or 0.0),
        fenced=[Fenced(**e) for e in raw.get("fenced") or []],
        absent=[Absent(**e) for e in raw.get("absent") or []],
    )


def main(argv: list[str] | None = None) -> int:
    import argparse  # noqa: PLC0415
    import sys  # noqa: PLC0415

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="/home/daniel/Storage",
                    help="directory holding the Hive checkouts")
    ap.add_argument("--only", action="append", default=None, metavar="SOURCE",
                    help="fence this source and leave the others; repeatable")
    ap.add_argument("--revert", action="store_true",
                    help="put every store back where it was")
    ap.add_argument("--inspect", action="store_true",
                    help="report whether the retired stores are unmoved")
    ap.add_argument("--journal", default=None)
    args = ap.parse_args(argv)
    journal = Path(args.journal) if args.journal else None

    if args.revert and args.inspect:
        print("REFUSED: --revert and --inspect are different questions.",
              file=sys.stderr)
        return 2

    if args.revert:
        try:
            report = lower_fence(journal)
        except FileNotFoundError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
        for entry in report.fenced:
            print(f"restored {entry.path}")
        for line in report.moved:
            print(f"  LEFT {line}")
        print(f"\n{len(report.fenced)} store(s) back where they were, "
              f"{len(report.moved)} left alone.")
        return 1 if report.moved else 0

    if args.inspect:
        try:
            report = inspect(journal)
        except FileNotFoundError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
        for entry in report.fenced:
            print(f"ok   {entry.source:<10} {entry.moved_to} "
                  f"({entry.size} bytes, unmoved)")
        for line in report.moved:
            print(f"FAIL {line}")
        if not report.fenced and not report.moved:
            print("The journal names no fenced store.")
            return 1
        print(f"\n{len(report.fenced)} retired store(s) unmoved, "
              f"{len(report.moved)} not.")
        return 1 if report.moved else 0

    report = raise_fence(args.base, sources=set(args.only) if args.only
                         else None, journal=journal)
    for entry in report.fenced:
        print(f"fenced {entry.source:<10} {entry.path}\n"
              f"       -> {entry.moved_to} ({entry.size} bytes)")
    for entry in report.absent:
        print(f"--     {entry.source:<10} {entry.path} is not here")
    if not report.fenced:
        print("\nNothing was fenced. Every store named is already fenced or "
              "absent.")
        return 1
    print(f"\n{len(report.fenced)} store(s) retired. A write to any of them "
          "now fails with 'unable to open database file'.")
    print(f"Recorded in {journal or journal_path()}.")
    print("\nThe way back:")
    for line in revert_commands(journal):
        print(f"  {line}" if line else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
