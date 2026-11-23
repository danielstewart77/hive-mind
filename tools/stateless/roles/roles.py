#!/usr/bin/env python3
"""The hive's roles, one operation per call, over the console's own roles
routes.

    roles.py list
    roles.py show ROLE
    roles.py create NAME [--slug S] [--model M] [--skills a,b]
                         [--tools shell,files] [--hooks x,y]
    roles.py update ROLE [--name N] [--model M | --clear-model]
                         [--skills a,b] [--tools ..] [--hooks ..]
    roles.py add-skill ROLE SKILL
    roles.py remove-skill ROLE SKILL
    roles.py delete ROLE
    roles.py keep ROLE
    roles.py promote ROLE
    roles.py take SLUG
    roles.py catalog

ROLE is a role's slug or its numeric id; either is resolved through the
console's own listing.

Prints one JSON object on stdout. Exit 0 on success; exit 1 with
`{"error": ..., "kind": ...}` on stdout when the operation was refused or
failed; exit 2, the same shape, for a malformed command line.

**Through the console's own routes.** A role decides what every mind holding
it may use, and the console is the one place that checks a tool name, keeps
a slug unique and refuses to delete the general role. The console is
`HIVE_SITE_URL` and the credential is `ROLES_PUSH_TOKEN`, the operator's own:
it admits what the Roles page admits and nothing else on the console. It is
never printed, not even in an error.

`update --skills a,b` replaces the whole list, so two callers replacing at
once overwrite each other. `add-skill` and `remove-skill` change one name
under a lock on the role and leave every other grant alone, and are what to
use to give a role one skill. An empty value (`--skills ""`) clears a list.
`update` sends only the fields it is given, so a field left out is a field
left as it was.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib import error, parse, request

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
                      "whose roles these are.")
    return url


def _token() -> str:
    token = (os.getenv("ROLES_PUSH_TOKEN") or "").strip()
    if not token:
        raise Refused("ROLES_PUSH_TOKEN is unset: it is the operator's "
                      "credential for roles, and without it the console "
                      "answers every call with 401.")
    return token


def _call(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """One roles route, with the operator's bearer.

    A 401 is a credential this install did not issue or did not match; a 403
    is a route the token does not reach. Telling them apart is the whole
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
                f"the console refused the operator's roles credential: {detail}"
            ) from exc
        if exc.code == 403:
            raise Refused(
                f"the roles token does not reach {method} {path}: {detail}"
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


# ── finding a role ─────────────────────────────────────────────────────


def _roles() -> list[dict[str, Any]]:
    return list((_call("GET", "/api/roles") or {}).get("roles") or [])


def _find(ref: str) -> dict[str, Any]:
    """The role a slug or a numeric id names, as the console lists it.

    A numeric reference is tried as an id first and then as a slug, since a
    role may be called "2024".
    """
    ref = str(ref).strip()
    rows = _roles()
    if ref.isdigit():
        for row in rows:
            if row.get("id") == int(ref):
                return row
    for row in rows:
        if row.get("slug") == ref:
            return row
    raise Refused(f"no role {ref!r} on this hive")


def _held(ref: str) -> int:
    """The id of a role that exists on this hive (not one only in the repo)."""
    row = _find(ref)
    if row.get("id") is None:
        raise Refused(f"role {row['slug']!r} is in the repo only: "
                      f"`take {row['slug']}` it into the hive first")
    return int(row["id"])


def _names(text: str | None) -> list[str] | None:
    """`a,b` as a list, `""` as the empty list, and a flag left out as None."""
    if text is None:
        return None
    return [part.strip() for part in text.split(",") if part.strip()]


# ── commands ───────────────────────────────────────────────────────────


def do_list(args) -> dict[str, Any]:
    del args
    rows = _roles()
    return {"count": len(rows), "roles": rows}


def do_show(args) -> dict[str, Any]:
    return _find(args.role)


def do_catalog(args) -> dict[str, Any]:
    del args
    return _call("GET", "/api/roles/catalog")


def _grants(args, *, into: dict[str, Any]) -> dict[str, Any]:
    for field in ("skills", "tools", "hooks"):
        names = _names(getattr(args, field, None))
        if names is not None:
            into[field] = names
    return into


def do_create(args) -> dict[str, Any]:
    body: dict[str, Any] = {"name": args.name}
    if args.slug is not None:
        body["slug"] = args.slug
    if args.model is not None:
        body["model"] = args.model
    return _call("POST", "/api/roles", _grants(args, into=body))


def do_update(args) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if args.name is not None:
        changes["name"] = args.name
    if args.clear_model:
        changes["model"] = None
    elif args.model is not None:
        changes["model"] = args.model
    _grants(args, into=changes)
    if not changes:
        raise Refused("nothing to update: give --name, --model, "
                      "--clear-model, --skills, --tools or --hooks")
    rid = _held(args.role)
    return _call("PUT", f"/api/roles/{rid}", changes)


def do_add_skill(args) -> dict[str, Any]:
    rid = _held(args.role)
    return _call("POST", f"/api/roles/{rid}/skills", {"skill": args.skill})


def do_remove_skill(args) -> dict[str, Any]:
    rid = _held(args.role)
    return _call("DELETE",
                 f"/api/roles/{rid}/skills/{parse.quote(args.skill, safe='')}")


def do_delete(args) -> dict[str, Any]:
    return _call("DELETE", f"/api/roles/{_held(args.role)}")


def do_keep(args) -> dict[str, Any]:
    return _call("POST", f"/api/roles/{_held(args.role)}/keep")


def do_promote(args) -> dict[str, Any]:
    return _call("POST", f"/api/roles/{_held(args.role)}/promote")


def do_take(args) -> dict[str, Any]:
    return _call("POST", f"/api/roles/repo/{parse.quote(args.slug, safe='')}/take")


# ── the command line ───────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="roles.py", description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)

    def add(name, run, help_, *positional):
        sub = subs.add_parser(name, help=help_)
        for arg in positional:
            sub.add_argument(arg)
        sub.set_defaults(run=run)
        return sub

    def grants(sub):
        sub.add_argument("--skills", help="comma-separated skill names")
        sub.add_argument("--tools", help="comma-separated built-in tools")
        sub.add_argument("--hooks", help="comma-separated hook names")

    add("list", do_list, "every role on this hive and in the repo")
    add("show", do_show, "one role", "role")
    add("catalog", do_catalog, "the models, skills and hooks a role can be given")

    create = add("create", do_create, "make a role", "name")
    create.add_argument("--slug", help="its key; defaults to the name's")
    create.add_argument("--model")
    grants(create)

    update = add("update", do_update, "change a role's fields", "role")
    update.add_argument("--name")
    model = update.add_mutually_exclusive_group()
    model.add_argument("--model")
    model.add_argument("--clear-model", dest="clear_model", action="store_true",
                       help="the role names no model")
    grants(update)

    add("add-skill", do_add_skill, "give a role one skill", "role", "skill")
    add("remove-skill", do_remove_skill, "take one skill off a role",
        "role", "skill")
    add("delete", do_delete, "delete a role; cannot be undone", "role")
    add("keep", do_keep, "keep the hive's copy against the repo's", "role")
    add("promote", do_promote, "write the hive's copy to the repo", "role")
    add("take", do_take, "take a repo role into the hive", "slug")
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
