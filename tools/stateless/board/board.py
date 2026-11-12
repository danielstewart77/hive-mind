#!/usr/bin/env python3
"""The hive's Kanban board, one operation per call, over the console's own
board routes.

    board.py list [--kind epic|feature|story] [--status S] [--parent ID]
    board.py show ITEM
    board.py relations ITEM
    board.py create epic --title T [--description D]
    board.py create feature --title T --epic ID [--description D]
    board.py create story --title T (--feature ID | --epic ID)
                          [--description D] [--before ID] [--status S]
    board.py update ITEM [--title T] [--description D] [--status S]
    board.py move ITEM --before OTHER
    board.py statuses

Prints one JSON object on stdout. Exit 0 on success; exit 1 with
`{"error": ..., "kind": ...}` on stdout when the operation was refused or
failed; exit 2, the same shape, for a malformed command line.

**Through the board's own routes, never raw SQL.** `work_items` is one
table, but the ordering, the hierarchy numbering and the write lock that
keeps two writers from interleaving all live in the console's board module
above it. A statement against the table skips every one of them: a story
inserted by hand has no position anybody agrees on, and the backlog numbers
the operator reads (`2.17.6`) are computed from an order it is not in.

The console is `HIVE_SITE_URL` and the credential is `BOARD_PUSH_TOKEN` —
issued for the board specifically. It admits the operations above and
nothing else on the console: not the memory store, not another Mind's
settings, and not the delete. There is no delete here because there is none
to reach: finished work goes to `done` and stays on the board.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib import error, parse, request

#: The lanes a story moves through, in the order it moves through them.
STATUSES = ("backlog", "committed", "in_progress", "done")

#: What each kind's collection is called in the board's own payload.
_COLLECTIONS = {"epic": "epics", "feature": "features", "story": "stories"}

_TIMEOUT = 30.0


class Refused(Exception):
    """An operation that ran and changed nothing the caller asked for."""


class _Parser(argparse.ArgumentParser):
    """A usage error is a JSON error too, not argparse's prose on stderr."""

    def error(self, message: str) -> None:  # type: ignore[override]
        print(json.dumps({"error": f"{self.prog}: {message}",
                          "kind": "UsageError"}))
        raise SystemExit(2)


# ── the console ────────────────────────────────────────────────────────


def _console() -> str:
    url = (os.getenv("HIVE_SITE_URL") or "").strip().rstrip("/")
    if not url:
        raise Refused("HIVE_SITE_URL is unset: it names the hive console "
                      "this board lives on.")
    return url


def _token() -> str:
    token = (os.getenv("BOARD_PUSH_TOKEN") or "").strip()
    if not token:
        raise Refused("BOARD_PUSH_TOKEN is unset: it is this mind's "
                      "credential for the board, and without it the "
                      "console answers every call with 401.")
    return token


def _call(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """One board route, with this mind's bearer.

    Every refusal the console makes is reported as what it was. A 401 is a
    credential this install did not issue or did not match, and a 403 is a
    route the board token does not reach — telling them apart is the whole
    difference between "rotate the token" and "that is not yours to do".
    """
    payload = json.dumps(body).encode() if body is not None else None
    req = request.Request(f"{_console()}{path}", data=payload, method=method)
    req.add_header("Authorization", f"Bearer {_token()}")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req, timeout=_TIMEOUT) as answer:
            text = answer.read().decode() or "null"
    except error.HTTPError as exc:
        detail = _detail(exc)
        if exc.code == 401:
            raise Refused(
                f"the console refused this mind's board credential: {detail}"
            ) from exc
        if exc.code == 403:
            raise Refused(
                f"the board token does not reach {method} {path}: {detail}"
            ) from exc
        raise Refused(f"{method} {path}: {exc.code} {detail}") from exc
    except error.URLError as exc:
        raise Refused(
            f"the console at {_console()} did not answer: {exc.reason}"
        ) from exc
    return json.loads(text)


def _detail(exc: error.HTTPError) -> str:
    try:
        return str(json.loads(exc.read().decode()).get("detail") or exc.reason)
    except (ValueError, AttributeError, OSError):
        return str(exc.reason)


# ── reads ──────────────────────────────────────────────────────────────


def _board() -> list[dict[str, Any]]:
    """Every row on the board, flat, each carrying its kind and its number.

    The board arrives as three collections in one read, so the rows cannot
    disagree about what is on it. The numbers are a second read, and a row
    the numbering does not have gets no `number` key rather than a made-up
    one: a wrong `2.17.6` in front of the operator is worse than none.
    """
    data = _call("GET", "/api/kanban/data") or {}
    numbers = _call("GET", "/api/kanban/numbers") or {}
    rows = []
    for kind, collection in _COLLECTIONS.items():
        for row in data.get(collection) or []:
            item = dict(row, kind=kind)
            if row.get("id") in numbers:
                item["number"] = numbers[row["id"]]
            rows.append(item)
    return rows


def _parent_of(row: dict[str, Any]) -> str | None:
    for key in ("parent_id", "feature_id", "epic_id"):
        if row.get(key):
            return str(row[key])
    return None


def do_list(args) -> dict[str, Any]:
    rows = _board()
    if args.kind:
        rows = [r for r in rows if r["kind"] == args.kind]
    if args.status:
        rows = [r for r in rows if r.get("status") == args.status]
    if args.parent:
        rows = [r for r in rows if _parent_of(r) == args.parent]
    rows.sort(key=lambda r: _number_key(r.get("number")))
    return {"count": len(rows), "items": rows}


def _number_key(number: str | None) -> tuple:
    """Backlog order, so a listing reads top to bottom as the board does.

    `2.17.6` sorts under `2.9.1` as text and above it as the operator reads
    it, so the parts are compared as integers. A row with no number sorts
    last rather than first: it is the odd one out and belongs where it is
    noticed, not at the top pretending to be next.
    """
    if not number:
        return (1,)
    parts = []
    for part in str(number).split("."):
        parts.append(int(part) if part.isdigit() else 0)
    return (0, parts)


def do_show(args) -> dict[str, Any]:
    for row in _board():
        if row["id"] == args.item:
            return row
    raise Refused(f"no item {args.item!r} on the board")


def do_relations(args) -> dict[str, Any]:
    return _call("GET", f"/api/kanban/relations/{parse.quote(args.item)}")


def do_statuses(args) -> dict[str, Any]:
    del args
    return {"statuses": list(STATUSES)}


# ── writes ─────────────────────────────────────────────────────────────


#: The prose fields a row carries. The board creates a row from its title
#: and parent alone and takes these on update, so they are written in a
#: second call rather than being refused as unsupported on the first.
_PROSE = ("description", "requirements", "acceptance_criteria", "qa_tests")


def _prose_of(args) -> dict[str, Any]:
    return {field: getattr(args, field) for field in _PROSE
            if getattr(args, field, None) is not None}


def do_create(args) -> dict[str, Any]:
    body: dict[str, Any] = {"title": args.title}
    if args.kind == "feature":
        body["epic_id"] = args.epic
    elif args.kind == "story":
        if (args.feature is None) == (args.epic is None):
            raise Refused("a story goes under exactly one of --feature "
                          "or --epic")
        body["feature_id" if args.feature else "epic_id"] = (
            args.feature or args.epic)
        if args.status:
            body["status"] = args.status

    created = _call("POST", f"/api/kanban/{_COLLECTIONS[args.kind]}", body)
    item_id = created["id"]

    # The prose and the placement are separate calls, and in this order: the
    # board reads a PATCH carrying `before_id` alone as the move, so a move
    # sent together with a description would be taken as a field update and
    # the row would never be placed. The row exists either way — a failure
    # here leaves it on the board to be finished, not lost.
    prose = _prose_of(args)
    if prose:
        created = _call("PATCH", _path(item_id, args.kind), prose)

    # A story created without its place in the sequence is invisible to
    # "what is next" — the question the board is read for.
    if getattr(args, "before", None):
        created = _call("PATCH", _path(item_id, args.kind),
                        {"before_id": args.before})
    return created


def _path(item_id: str, kind: str) -> str:
    """The update route. The kind in the URL is what the caller last read
    the row as; the console resolves the row's actual kind itself, so a row
    that changed type under us is still updated rather than 404'd."""
    return f"/api/kanban/{_COLLECTIONS[kind]}/{parse.quote(item_id)}"


def do_update(args) -> dict[str, Any]:
    changes: dict[str, Any] = _prose_of(args)
    for field in ("title", "status"):
        value = getattr(args, field, None)
        if value is not None:
            changes[field] = value
    if not changes:
        raise Refused("nothing to update: give --title, --status or one of "
                      "--description, --requirements, --acceptance-criteria, "
                      "--qa-tests")
    kind = do_show(argparse.Namespace(item=args.item))["kind"]
    return _call("PATCH", _path(args.item, kind), changes)


def do_move(args) -> dict[str, Any]:
    kind = do_show(argparse.Namespace(item=args.item))["kind"]
    return _call("PATCH", _path(args.item, kind), {"before_id": args.before})


# ── the command line ───────────────────────────────────────────────────


def _add_prose(parser: argparse.ArgumentParser) -> None:
    """The prose fields, on every command that writes a row.

    A feature's description is not decoration: it carries its stories in
    order, with what each contributes and what blocks what, and the board's
    positions are only the order — not the why. A feature whose description
    does not name its stories in order is incomplete.
    """
    parser.add_argument("--description")
    parser.add_argument("--requirements")
    parser.add_argument("--acceptance-criteria", dest="acceptance_criteria")
    parser.add_argument("--qa-tests", dest="qa_tests")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="board.py", description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)

    listing = subs.add_parser("list", help="rows on the board, in order")
    listing.add_argument("--kind", choices=sorted(_COLLECTIONS))
    listing.add_argument("--status", choices=STATUSES)
    listing.add_argument("--parent", help="only rows under this item")
    listing.set_defaults(run=do_list)

    show = subs.add_parser("show", help="one row, with its backlog number")
    show.add_argument("item")
    show.set_defaults(run=do_show)

    relations = subs.add_parser("relations", help="parent, children, choices")
    relations.add_argument("item")
    relations.set_defaults(run=do_relations)

    create = subs.add_parser("create", help="add an epic, feature or story")
    kinds = create.add_subparsers(dest="kind", required=True)

    epic = kinds.add_parser("epic")
    epic.add_argument("--title", required=True)
    _add_prose(epic)
    epic.set_defaults(run=do_create, kind="epic")

    feature = kinds.add_parser("feature")
    feature.add_argument("--title", required=True)
    feature.add_argument("--epic", required=True, help="the epic it is under")
    _add_prose(feature)
    feature.set_defaults(run=do_create, kind="feature")

    story = kinds.add_parser("story")
    story.add_argument("--title", required=True)
    story.add_argument("--feature", help="the feature it is under")
    story.add_argument("--epic", help="or the epic, when it has no feature")
    story.add_argument("--status", choices=STATUSES)
    story.add_argument("--before", help="the row it goes before")
    _add_prose(story)
    story.set_defaults(run=do_create, kind="story")

    update = subs.add_parser("update", help="change a row's fields")
    update.add_argument("item")
    update.add_argument("--title")
    update.add_argument("--status", choices=STATUSES)
    _add_prose(update)
    update.set_defaults(run=do_update)

    move = subs.add_parser("move", help="put a row before another")
    move.add_argument("item")
    move.add_argument("--before", required=True)
    move.set_defaults(run=do_move)

    statuses = subs.add_parser("statuses", help="the lanes a story moves in")
    statuses.set_defaults(run=do_statuses)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        print(json.dumps(args.run(args), indent=2, sort_keys=True))
    except Refused as exc:
        print(json.dumps({"error": str(exc), "kind": "Refused"}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
