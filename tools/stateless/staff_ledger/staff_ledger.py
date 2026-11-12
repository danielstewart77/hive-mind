#!/usr/bin/env python3
"""The chief of staff's ledger, one operation per call, over `hive_data.staff`.

    staff_ledger.py open --name N --session S [--description D] [--status S]
    staff_ledger.py show TASK
    staff_ledger.py list [--status S]          each task with its session ids
    staff_ledger.py set-status TASK STATUS [--expect S]
    staff_ledger.py edit TASK [--name N] [--description D]
    staff_ledger.py attach TASK SESSION
    staff_ledger.py detach TASK SESSION
    staff_ledger.py find SESSION
    staff_ledger.py statuses
    staff_ledger.py delete TASK

Prints one JSON object on stdout. Exit 0 on success; exit 1 with
`{"error": ..., "kind": ...}` on stdout when the operation was refused or
failed, including a task that is not there and a `--expect` the task has
left; exit 2, the same shape, for a malformed command line.

The database is `HIVE_DATABASE_URL`. The chief of staff connects as a role
that sees only the `staff` schema, so the pool opens without
`hive_data.connect()`'s whole-database schema check, which would read every
other schema as missing.

`open` creates the task and attaches its session in one transaction: a
session already working another task refuses the open and leaves no task
behind.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from typing import Any

import hive_data
from hive_data import staff

#: The status a task opens in unless told otherwise.
OPENED = "in progress"


class Refused(Exception):
    """An operation that ran and changed nothing the caller asked for."""


def _session_id(value: str) -> str:
    """A hive-comms session id, which comms mints as a UUID.

    Checked here because a failed `POST /sessions` piped through `jq -r .id`
    is the string `null`, and the ledger would otherwise watch it.
    """
    try:
        uuid.UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a hive-comms session id (a UUID)") from None
    return value


class _Parser(argparse.ArgumentParser):
    """A usage error is a JSON error too, not argparse's prose on stderr."""

    def error(self, message: str) -> None:  # type: ignore[override]
        print(json.dumps({"error": f"{self.prog}: {message}",
                          "kind": "UsageError"}))
        raise SystemExit(2)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="staff_ledger.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    opened = sub.add_parser("open", help="create a task with its session")
    opened.add_argument("--name", required=True)
    opened.add_argument("--session", required=True, type=_session_id,
                        help="the hive-comms session id working it")
    opened.add_argument("--description", default="")
    opened.add_argument("--status", default=OPENED)

    sub.add_parser("show", help="one task and its sessions").add_argument(
        "task")
    listed = sub.add_parser("list", help="every task, or those in a status")
    listed.add_argument("--status")

    moved = sub.add_parser("set-status", help="move a task's status")
    moved.add_argument("task")
    moved.add_argument("status")
    moved.add_argument("--expect",
                       help="only if the task is still in this status")

    edited = sub.add_parser("edit", help="rename or redescribe a task")
    edited.add_argument("task")
    edited.add_argument("--name")
    edited.add_argument("--description")

    for name in ("attach", "detach"):
        pair = sub.add_parser(name, help=f"{name} a session")
        pair.add_argument("task")
        pair.add_argument("session", type=_session_id)

    sub.add_parser("find", help="the task a session works").add_argument(
        "session", type=_session_id)
    sub.add_parser("statuses", help="every status a task may have")
    sub.add_parser("delete", help="remove a task opened in error"
                   ).add_argument("task")
    return parser


async def _show(task_id: str) -> dict[str, Any]:
    task = await staff.task(task_id)
    if task is None:
        raise Refused(f"no task {task_id!r}")
    return {"task": task, "sessions": await staff.task_sessions(task_id)}


async def _changed(task_id: str, row: dict[str, Any] | None,
                   expect: str | None = None) -> dict[str, Any]:
    if row is not None:
        return {"task": row}
    if await staff.task(task_id) is None:
        raise Refused(f"no task {task_id!r}")
    if expect is not None and expect not in await staff.statuses():
        # Otherwise a misspelt --expect reads as the task having moved.
        raise Refused(f"{expect!r} is not a status; task {task_id!r} "
                      "unchanged")
    raise Refused(f"task {task_id!r} is no longer {expect!r}; unchanged")


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    command = args.command
    if command == "open":
        async with hive_data.transaction() as unit:
            task = await staff.create_task(
                args.name, args.status, description=args.description,
                unit=unit)
            await staff.attach_session(task["id"], args.session, unit=unit)
        return {"id": task["id"], "task": task, "session": args.session}
    if command == "show":
        return await _show(args.task)
    if command == "list":
        return {"tasks": [
            {**task, "sessions": [
                p["session_id"] for p in await staff.task_sessions(task["id"])]}
            for task in await staff.tasks(status=args.status)]}
    if command == "set-status":
        guard = {} if args.expect is None else {"expected_status": args.expect}
        row = await staff.update_task(args.task, status=args.status, **guard)
        return await _changed(args.task, row, args.expect)
    if command == "edit":
        change = {k: v for k, v in (("name", args.name),
                                    ("description", args.description))
                  if v is not None}
        if not change:
            raise Refused("edit names nothing to change: give --name or "
                          "--description")
        return await _changed(args.task, await staff.update_task(
            args.task, **change))
    if command == "attach":
        await staff.attach_session(args.task, args.session)
        return {"task": args.task, "session": args.session, "attached": True}
    if command == "detach":
        out = await staff.detach_session(args.task, args.session)
        if not out.get("rows"):
            raise Refused(f"session {args.session!r} is not attached to "
                          f"task {args.task!r}")
        return {"task": args.task, "session": args.session, "detached": True}
    if command == "find":
        return {"task": await staff.task_for_session(args.session)}
    if command == "statuses":
        return {"statuses": await staff.statuses()}
    if command == "delete":
        out = await staff.delete_task(args.task)
        if not out.get("rows"):
            raise Refused(f"no task {args.task!r}")
        return {"deleted": args.task}
    raise Refused(f"unknown command {command!r}")


async def _main(args: argparse.Namespace) -> dict[str, Any]:
    await hive_data.connect(verify=False)
    try:
        return await _run(args)
    finally:
        await hive_data.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        out = asyncio.run(_main(args))
    except Exception as exc:  # every failure is reported, none is raised
        print(json.dumps({"error": str(exc), "kind": type(exc).__name__}))
        return 1
    print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
