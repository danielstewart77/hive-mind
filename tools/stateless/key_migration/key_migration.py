#!/usr/bin/env python3
"""Move existing minds onto one key, account for every old-key holder, retire the old keys.

    key_migration.py report
    key_migration.py move MIND
    key_migration.py undo MIND
    key_migration.py tools-credential HOLDER
    key_migration.py retire
    key_migration.py end-person-keys

Run by the operator, never by a mind. Prints one JSON object on stdout. Exit 0
on success; exit 1 with `{"error": ..., "kind": ...}` when the console refused
or the operation did not run (`retire` while any mind is not moved lists what
is not, and exits 1); exit 2, the same shape, for a malformed command line.

**Through the console's own routes, never a second writer.** The console's
rotation lock is in-process, and this command is a separate process: writing
the env file or the services from here would interleave with a mind's creation
or a key replacement and leave the files and the services disagreeing. So it
only asks the console, which holds the lock, the journal and the backup. The
console is `HIVE_SITE_URL` and the credential is `HIVE_OPERATOR_KEY`: the
operator's own key as a person (an administrator's), which no mind holds. The
console recognises it from the hive's record of keys; it has no setting for it.

What it prints: names, files, variable names and digest prefixes at most, as
the console gives them. A key never appears in anything this command reads
from the console, and it does not read one itself.

`end-person-keys` is the one-time step of story 914, run once after the host
is on the app's own sign-in: it ends every person's key made the old way, deletes
the files that held them, and names each person it ended (safe to run twice).
Everyone signs in again afterwards. A mind's key is never touched.

`move` registers the proxy key the mind already holds with the gateway, the
memory store and the tools as that mind. Its old per-service values stop being
accepted at once but stay in their file until `retire`; `undo` puts a moved mind
back on them. `retire` ends all that: afterwards a mind cannot go back a
release, because the previous release's minds present values that are gone, and
it says so in its own output.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib import error, parse, request

_TIMEOUT = 600.0
_BASE = "/api/settings/key-migration"

#: Printed with every `retire` answer and every refusal, in the console's words too.
RETIRE_NOTICE = (
    "Retiring the old keys ends going back a release for every moved mind: "
    "the previous release's minds present per-service values that are gone, "
    "and no mind can be put back on them.")


class Refused(Exception):
    """An operation that ran and changed nothing the caller asked for."""

    def __init__(self, message: str, extra: dict[str, Any] | None = None):
        super().__init__(message)
        self.extra = extra or {}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        print(json.dumps({"error": f"{self.prog}: {message}", "kind": "UsageError"}))
        raise SystemExit(2)


def _console() -> str:
    url = (os.getenv("HIVE_SITE_URL") or "").strip().rstrip("/")
    if not url:
        raise Refused("HIVE_SITE_URL is unset: it names the hive console to ask.")
    return url


def _token() -> str:
    token = (os.getenv("HIVE_OPERATOR_KEY") or "").strip()
    if not token:
        raise Refused("HIVE_OPERATOR_KEY is unset: it is an administrator's own key, "
                      "for these routes, and without it the console answers 401.")
    return token


def _call(method: str, path: str) -> Any:
    req = request.Request(f"{_console()}{_BASE}{path}", method=method,
                          data=b"" if method == "POST" else None)
    req.add_header("Authorization", f"Bearer {_token()}")
    try:
        with request.urlopen(req, timeout=_TIMEOUT) as answer:
            return json.loads(answer.read().decode() or "null")
    except error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode() or "{}")
        except ValueError:
            body = {}
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(body, dict) and body.get("blocking"):
            raise Refused("retirement did not run: " + "; ".join(body["blocking"]),
                          {"blocking": body["blocking"], "notice": RETIRE_NOTICE}) from exc
        if exc.code == 401:
            raise Refused("the console refused this credential (401)") from exc
        if exc.code == 403:
            raise Refused("the console does not admit this credential on that route (403)") from exc
        raise Refused(str(detail or f"the console answered {exc.code}")) from exc
    except error.URLError as exc:
        raise Refused(f"the console could not be reached: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise Refused("the console did not finish answering; the operation may or may not have "
                      "landed. Run `report` before trying again.") from exc
    except ValueError as exc:
        raise Refused("the console's answer was not readable; the operation may or may not have "
                      "landed. Run `report` before trying again.") from exc


def _name(parser_value: str) -> str:
    return parse.quote(parser_value.strip().lower(), safe="")


def run(argv: list[str]) -> dict[str, Any]:
    parser = _Parser(prog="key_migration")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    sub.add_parser("report")
    for command in ("move", "undo"):
        sub.add_parser(command).add_argument("mind")
    sub.add_parser("tools-credential").add_argument("holder")
    sub.add_parser("retire")
    sub.add_parser("end-person-keys")
    args = parser.parse_args(argv)
    if args.command == "report":
        return _call("GET", "")
    if args.command == "move":
        return _call("POST", f"/{_name(args.mind)}/move")
    if args.command == "undo":
        return _call("POST", f"/{_name(args.mind)}/undo")
    if args.command == "tools-credential":
        return _call("POST", f"/tools-credential/{_name(args.holder)}")
    if args.command == "end-person-keys":
        return _call("POST", "/end-old-person-keys")
    result = _call("POST", "/retire")
    if isinstance(result, dict):
        result.setdefault("notice", RETIRE_NOTICE)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        print(json.dumps(run(list(sys.argv[1:] if argv is None else argv)), indent=2))
        return 0
    except Refused as exc:
        print(json.dumps({"error": str(exc), "kind": "Refused", **exc.extra}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
