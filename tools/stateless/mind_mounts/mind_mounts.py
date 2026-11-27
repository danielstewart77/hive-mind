#!/usr/bin/env python3
"""Which minds on this machine can read a secret that is not their own.

    mind_mounts.py check [--root DIR] [--compose FILE ...] [--env-file FILE]
                         [--key-root DIR] [--secret PATH ...]
                         [--operators NAME_OR_ID,...] [--strict-pending] [--map HOST=LOCAL ...]

Feature 834, requirement 33. A mind that does not hold the operator role may
see its own mind folder, its own key folder and a copy of the code with no
secret in it, and is handed no credential at all: its one key is a file. This
reads each mind's container file and names every mind that breaks that, with
the line that does.

What it lists, for a mind without the operator role:

- a mount whose source is, contains or lies inside: an install settings file
  (`.env`, `.env.<anything>`, `*.env`, `secrets.env`, in the install root, in
  `nervous-system/`, in each of `modules/*/`), the shared keyring, the services' data folders and the installer's `state/`,
  the key root (any key folder but its own), another mind's folder, the
  host's Docker socket, or a path named with `--secret`. A secret inside a
  mounted folder does not count when the same file hides it: a `tmpfs` on, or
  `/dev/null` mounted over, the place it would appear in the container (so the
  checkout root is listed only if it carries a settings file or another mind's
  folder that nothing hides);
- a mount of a file named `session_token`;
- an `env_file`, or an environment entry whose name says it is a credential
  (token, secret, password, key, database address ...) and that is not empty.
  The mind's one key reaches it as the file named by `MIND_KEY_FILE`, which is
  not a credential and is allowed.

Operator minds are never limited: they are left out of the findings and named
under `operators`. Which minds are operators is read from the hive database
(`HIVE_DATABASE_URL`, tables board.mind_roles and board.roles); `--operators`
names them instead. If neither can be had the check stops with exit 2 rather
than guess.

A value is never printed: an environment entry is shown by name only, a mount
as written in the file.

`pending` holds what the check sees and does not fail on, because removing it
needs work that belongs to a later story: `MIND_PROACTIVE_TOKEN`, the value a
mind's own chat bot presents (the bots story removes it).

Prints one JSON object on stdout. Exit 0 when no mind without the operator role
is listed; exit 1 when one is; exit 2 (the same JSON shape with `error`) when it
could not run.

Where relative paths and `${VARIABLES}` point: a variable is read from the
environment, then from `--env-file` (default `<root>/.env`); a path left
relative is relative to the compose file; `HOST_PROJECT_DIR` is taken as this root
and `HOST_DEV_DIR` as its parent, since the files name host paths. A path that cannot be worked out is itself a
finding: the check will not call a mount safe it cannot place.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

#: Named in the output, not failing: removed by a later story.
PENDING_ENV = {
    "MIND_PROACTIVE_TOKEN": "the credential a mind's own chat bot presents; "
                            "the bots story removes it",
}
#: Environment names that look like credentials and are not.
ALLOWED_ENV = {"MIND_KEY_FILE", "KEY_RING", "PYTHON_KEYRING_BACKEND"}
_CREDENTIAL_NAME = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|BEARER|CREDENTIAL|DATABASE_URL|DATABASE_URI|DSN|API_?KEY|ACCESS_KEY|PRIVATE_KEY|_PAT$|_KEY$|^KEY$")
#: Names that carry the word but name a setting, not a secret.
_NOT_CREDENTIAL_SUFFIX = ("_KEYRING_KEY", "_CONFIGURES_TOOLS")
_DOCKER_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock")
_SKIP_SUFFIXES = (".example", ".sample", ".template", ".dist")
#: Folders of a minds/ directory that are not a mind.
_NOT_MINDS = {"example", "harness", "__pycache__"}


class Stop(Exception):
    """The check could not run; nothing was concluded."""


# ── ${VARIABLE} resolution ────────────────────────────────────────────


def _env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[name] = value
    return out


class Unresolved(Exception):
    pass


def expand(text: str, env: dict[str, str]) -> str:
    """Compose's `${NAME}`, `${NAME:-default}`, `${NAME-default}` and
    `${NAME:?message}`, defaults nested. A required name with no value raises
    `Unresolved`."""
    out: list[str] = []
    i = 0
    while i < len(text):
        if text.startswith("${", i):
            depth, j = 1, i + 2
            while j < len(text) and depth:
                depth += {"{": 1, "}": -1}.get(text[j], 0)
                j += 1
            if depth:
                raise Unresolved(text)
            body = text[i + 2:j - 1]
            m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?])(.*))?$", body, re.S)
            if not m:
                raise Unresolved(text)
            name, op, arg = m.groups()
            value = env.get(name)
            if op == ":-":
                out.append(value if value else expand(arg, env))
            elif op == "-":
                out.append(value if value is not None else expand(arg, env))
            elif op and op.endswith("?"):
                if not value:
                    raise Unresolved(name)
                out.append(value)
            else:
                if value is None:
                    raise Unresolved(name)
                out.append(value)
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


# ── paths ─────────────────────────────────────────────────────────────


def _norm(path: str | Path, root: Path, maps: list[tuple[str, str]] = ()) -> str:
    p = os.path.expanduser(str(path))
    if not os.path.isabs(p):
        p = os.path.join(str(root), p)
    p = os.path.normpath(p)
    for host, local in maps:
        if _within(p, host):
            p = os.path.normpath(os.path.join(local, os.path.relpath(p, host)))
            break
    return os.path.realpath(p)


def _within(inner: str, outer: str) -> bool:
    return inner == outer or inner.startswith(outer.rstrip(os.sep) + os.sep)


def _settings_files(tree: Path) -> list[str]:
    found: list[str] = []
    for pattern in (".env", ".env.*", "*.env", "nervous-system/.env", "nervous-system/.env.*",
                    "nervous-system/*.env", "modules/*/.env", "modules/*/.env.*",
                    "modules/*/*.env", "state/secrets.env"):
        for hit in glob.glob(str(tree / pattern)):
            if os.path.isfile(hit) and not hit.endswith(_SKIP_SUFFIXES):
                found.append(os.path.realpath(hit))
    return sorted(set(found))


def _data_folders(tree: Path) -> list[str]:
    """Folders that hold other minds' secrets or the services' own state: the
    shared keyring, the memory store's and gateway's data, each module's data."""
    hits = [str(tree / "data" / "keyring"), str(tree / "nervous-system" / "data"),
            str(tree / "state"), *glob.glob(str(tree / "modules" / "*" / "data"))]
    return sorted({os.path.realpath(h) for h in hits if os.path.isdir(h)})


def _mind_folders(minds: Path) -> list[str]:  # noqa: D103
    if not minds.is_dir():
        return []
    return sorted(os.path.realpath(d) for d in glob.glob(str(minds / "*"))
                  if os.path.isdir(d) and os.path.basename(d) not in _NOT_MINDS
                  and os.path.isfile(os.path.join(d, "runtime.yaml")))


def _folder_case(parent: Path, name: str) -> str:
    """The folder of that name however the compose file capitalises it."""
    try:
        for entry in os.listdir(parent):
            if entry.lower() == name.lower():
                return entry
    except OSError:
        pass
    return name


class Secrets:
    """What a mind without the operator role must not reach, on this machine."""

    def __init__(self, root: Path, key_root: Path, extra: list[str]):
        self.root = root
        self.key_root = key_root
        trees = [root]
        clone = root / "repos" / "hive-mind"
        if clone.is_dir():
            trees.append(clone)
        self.files: list[tuple[str, str]] = []   # (path, reason)
        self.folders: list[tuple[str, str]] = []
        for tree in trees:
            self.files += [(f, "an install settings file") for f in _settings_files(tree)]
            self.folders += [(d, "a folder of the install's own state") for d in _data_folders(tree)]
        # A folder that holds the checkout holds its neighbours too: their
        # settings files count (the hive site's, the authentication service's).
        for sibling in sorted(glob.glob(str(root.parent / "*")))[:300]:
            if os.path.isdir(sibling) and os.path.realpath(sibling) != str(root) \
                    and not _within(str(root), os.path.realpath(sibling)):
                self.files += [(f, "an install settings file")
                               for f in _settings_files(Path(sibling))]
        for path in extra:
            self.files.append((_norm(path, root), "a secret you named"))
        for sock in _DOCKER_SOCKETS:
            self.files.append((sock, "the host's Docker socket"))
        self.minds_dir = root / "minds"

    def reasons(self, source: str, target: str, masks: list[str], mind: str) -> list[str]:
        """Why a mount of `source` at `target` reaches a secret, given the
        container paths `masks` that hide whatever is under them."""
        own_folder = os.path.realpath(self.minds_dir / _folder_case(self.minds_dir, mind))
        own_keys = os.path.realpath(self.key_root / mind)

        def hidden(inside: str) -> bool:
            if not (_within(inside, source) and target):
                return False
            seen = os.path.normpath(os.path.join(target, os.path.relpath(inside, source)))
            # A mask hides only what it is laid over: it must lie inside this
            # mount (a tmpfs on a parent is covered by the bind laid on top).
            return any(_within(seen, m) and _within(m, os.path.normpath(target))
                       for m in masks)

        why: list[str] = []
        for path, reason in (*self.files, *self.folders):
            if _within(path, own_folder):
                continue
            if _within(path, source):
                if not hidden(path):
                    why.append(f"{reason} ({path})")
            elif _within(source, path) and os.path.isdir(path):
                why.append(f"{reason} ({path})")
        for other in _mind_folders(self.minds_dir):
            if other == own_folder:
                continue
            if _within(other, source):
                if not hidden(other):
                    why.append(f"another mind's folder ({other})")
            elif _within(source, other):
                why.append(f"another mind's folder ({other})")
        if os.path.isfile(os.path.join(source, ".credentials.json")) \
                and not _within(source, own_folder):
            why.append(f"a login on the host ({source}/.credentials.json)")
        keys = os.path.realpath(self.key_root)
        if not os.path.isdir(keys):
            pass
        elif _within(keys, source) and keys != own_keys:
            if not hidden(keys):
                why.append(f"a key folder that is not its own ({keys})")
        elif _within(source, keys) and not _within(source, own_keys):
            why.append(f"a key folder that is not its own ({source})")
        return why


# ── the container files ───────────────────────────────────────────────


def _split_volume(text: str) -> list[str]:
    """`source:target:mode` split on the colons that are not inside `${...}`."""
    parts, buf, depth = [], [], 0
    for i, ch in enumerate(text):
        if text.startswith("${", i):
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
        if ch == ":" and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _volumes(service: dict) -> list[tuple[str, str, str]]:
    """(source as written, target, entry text) of every host path a service mounts."""
    out: list[tuple[str, str, str]] = []
    for entry in service.get("volumes") or []:
        if isinstance(entry, dict):
            if entry.get("type") == "bind" and entry.get("source"):
                out.append((str(entry["source"]), str(entry.get("target") or ""),
                            json.dumps(entry, sort_keys=True)))
            continue
        text = str(entry)
        parts = _split_volume(text)
        source = parts[0]
        target = parts[1] if len(parts) > 1 else ""
        if source and (source[0] in "/.~$"):
            out.append((source, target, text))
        # a name with none of those is a named volume: not a host path
    return out


def _tmpfs(service: dict) -> list[str]:
    raw = service.get("tmpfs") or []
    raw = [raw] if isinstance(raw, (str, dict)) else raw
    out = []
    for t in raw:
        if isinstance(t, dict):
            if t.get("target"):
                out.append(os.path.normpath(str(t["target"])))
        else:
            out.append(os.path.normpath(str(t).split(":")[0]))
    return out


def _environment(service: dict) -> list[tuple[str, str, bool]]:
    """(name, text shown, has a value) for each environment entry."""
    env = service.get("environment") or []
    out = []
    if isinstance(env, dict):
        for name, value in env.items():
            # A null value passes the host's own value through: treated as set.
            has = value is None or str(value).strip() != ""
            out.append((str(name), str(name), has))
        return out
    for item in env:
        name, sep, value = str(item).partition("=")
        # `NAME` alone passes the host's value through: treated as set.
        out.append((name, name, (value.strip() != "") if sep else True))
    return out


def _line_of(text: str, needle: str) -> int | None:
    for number, line in enumerate(text.splitlines(), 1):
        if needle and needle in line:
            return number
    return None


def _mind_services(path: Path, known: set[str] = frozenset()) -> list[tuple[str, dict, str]]:
    try:
        raw = path.read_text()
        doc = yaml.safe_load(raw) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise Stop(f"cannot read {path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise Stop(f"{path} is not a compose file")
    services = doc.get("services") or {}
    if not isinstance(services, dict):
        raise Stop(f"{path} has a services section that is not a mapping")
    # A service is a mind's when it declares MIND_NAME or is named like a mind
    # folder of this install: an override file that only adds a mount to a
    # mind's service is exactly where a secret would slip in.
    return [(key, service, raw) for key, service in services.items()
            if isinstance(service, dict)
            and ("MIND_NAME" in {n for n, _, _ in _environment(service)}
                 or str(key).lower() in known)]


def _mind_name(service: dict, env: dict[str, str], key: str) -> str:
    environment = service.get("environment") or []
    pairs = environment.items() if isinstance(environment, dict) else \
        (str(e).partition("=")[::2] for e in environment)
    for name, value in pairs:
        if name == "MIND_NAME" and value:
            try:
                return expand(str(value), env)
            except Unresolved:
                break
    return key


def _mind_id(service: dict, env: dict[str, str], root: Path, name: str) -> str:
    environment = service.get("environment") or []
    pairs = environment.items() if isinstance(environment, dict) else \
        (str(e).partition("=")[::2] for e in environment)
    for var, value in pairs:
        if var == "MIND_ID" and value:
            try:
                return expand(str(value), env)
            except Unresolved:
                break
    try:
        runtime = yaml.safe_load((root / "minds" / name / "runtime.yaml").read_text()) or {}
        return str(runtime.get("mind_id") or name) if isinstance(runtime, dict) else name
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return name


# ── who holds the operator role ───────────────────────────────────────


async def _operator_ids(dsn: str) -> set[str]:
    import asyncpg

    conn = await asyncpg.connect(dsn, timeout=15)
    try:
        rows = await conn.fetch(
            "SELECT m.mind_id FROM board.mind_roles m "
            "JOIN board.roles r ON r.id = m.role_id WHERE r.slug = 'operator'")
    finally:
        await conn.close()
    return {str(r["mind_id"]) for r in rows}


def operators(named: str | None) -> set[str]:
    if named is not None:
        return {n.strip() for n in named.split(",") if n.strip()}
    dsn = (os.environ.get("HIVE_DATABASE_URL") or "").strip()
    if not dsn:
        raise Stop("cannot tell which minds hold the operator role: HIVE_DATABASE_URL "
                   "is unset (or name them with --operators)")
    try:
        return asyncio.run(_operator_ids(dsn))
    except Exception as exc:
        raise Stop(f"cannot read the operator role from the hive database "
                   f"({type(exc).__name__}); name them with --operators") from exc


# ── the check ─────────────────────────────────────────────────────────


def check(root: Path, compose_files: list[Path], env_file: Path | None,
          key_root: Path | None, extra_secrets: list[str],
          operator_set: set[str], maps: list[tuple[str, str]] = ()) -> dict[str, Any]:
    env = {**_env_file(env_file or root / ".env"), **os.environ}
    # Where this machine's paths are, as the compose files name them: the
    # install folder (HOST_PROJECT_DIR) is this root, whatever the host calls it.
    maps = [*maps]
    if env.get("HOST_PROJECT_DIR"):
        maps.append((os.path.normpath(env["HOST_PROJECT_DIR"]), str(root)))
    if env.get("HOST_DEV_DIR"):
        maps.append((os.path.normpath(env["HOST_DEV_DIR"]), str(root.parent)))
    keys = key_root or (Path(env["HOST_KEY_DIR"]) if env.get("HOST_KEY_DIR") else root / "keys")
    secrets = Secrets(root, Path(_norm(keys, root, maps)), extra_secrets)
    findings: list[dict] = []
    pending: list[dict] = []
    skipped: list[str] = []
    checked: list[str] = []

    minds_dir = root / "minds"
    known = ({p.name.lower() for p in minds_dir.iterdir() if p.is_dir()}
             if minds_dir.is_dir() else set())
    for path in compose_files:
        for key, service, raw in _mind_services(path, known):
            name = _mind_name(service, env, key)
            ident = _mind_id(service, env, root, name)
            if ident in operator_set or name in operator_set:
                skipped.append(name)
                continue
            checked.append(name)

            def add(into: list, kind: str, entry: str, reason: str) -> None:
                # One finding per line of the file; its reasons are listed on it.
                for found in into:
                    if (found["mind"], found["file"], found["entry"]) == (name, str(path), entry):
                        if reason not in found["reasons"]:
                            found["reasons"].append(reason)
                        return
                into.append({"mind": name, "file": str(path), "line": _line_of(raw, entry),
                             "entry": entry, "kind": kind, "reasons": [reason]})

            volumes = _volumes(service)
            # What hides a place in the container: a tmpfs on it, or /dev/null
            # mounted over it.
            masks = _tmpfs(service) + [os.path.normpath(t) for s_, t, _ in volumes
                                       if os.path.normpath(s_) == "/dev/null" and t]
            for source, target, text in volumes:
                if os.path.basename(source.rstrip("/")) == "session_token":
                    add(findings, "mount", text, "mounts a session_token file: not its one key")
                try:
                    where = _norm(expand(source, env), path.parent, maps)
                except Unresolved as exc:
                    add(findings, "mount", text,
                        f"cannot work out where this points ({exc}); it is not called safe")
                    continue
                if os.path.normpath(source) == "/dev/null":
                    continue
                for why in secrets.reasons(where, target, masks, name):
                    add(findings, "mount", text, f"reaches {why}")
            for follow in ("volumes_from", "extends"):
                if service.get(follow):
                    add(findings, "mount", follow,
                        f"uses {follow}: mounts it brings in are not followed, so it is not called safe")
            if service.get("env_file"):
                add(findings, "environment", "env_file",
                    "is handed a settings file as its environment")
            for var, text, has_value in _environment(service):
                if not has_value or var in ALLOWED_ENV or var.endswith(_NOT_CREDENTIAL_SUFFIX):
                    continue
                if var in PENDING_ENV:
                    add(pending, "environment", text, PENDING_ENV[var])
                elif _CREDENTIAL_NAME.search(var.upper()):
                    add(findings, "environment", text,
                        "is handed a credential that is not its one key file (value not shown)")
    if not checked and not skipped:
        raise Stop(f"found no mind container file to check under {root}: wrong --root?")
    return {"ok": not findings, "root": str(root), "checked": sorted(set(checked)),
            "operators": sorted(set(skipped)), "findings": findings, "pending": pending}


def _default_compose(root: Path) -> list[Path]:
    files = sorted(Path(p) for p in glob.glob(str(root / "minds" / "*" / "container" / "compose.yaml")))
    stack = root / "docker-compose.yml"
    return ([stack] if stack.is_file() else []) + files


def _parser() -> argparse.ArgumentParser:
    class _P(argparse.ArgumentParser):
        def error(self, message: str) -> None:  # type: ignore[override]
            print(json.dumps({"error": f"{self.prog}: {message}", "kind": "UsageError"}))
            raise SystemExit(2)

    p = _P(prog="mind_mounts.py")
    sub = p.add_subparsers(dest="command", required=True, parser_class=_P)
    c = sub.add_parser("check")
    c.add_argument("--root", default=str(Path(__file__).resolve().parents[3]))
    c.add_argument("--compose", action="append", default=[])
    c.add_argument("--env-file")
    c.add_argument("--key-root")
    c.add_argument("--secret", action="append", default=[])
    c.add_argument("--operators")
    c.add_argument("--strict-pending", action="store_true",
                   help="also fail on what `pending` lists")
    c.add_argument("--map", action="append", default=[], metavar="HOST=LOCAL",
                   help="a host folder as the compose files name it, and where it is "
                        "here (HOST_PROJECT_DIR and HOST_DEV_DIR are mapped on their own)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = Path(os.path.realpath(args.root))
        files = [Path(f) for f in args.compose] or _default_compose(root)
        report = check(root, files, Path(args.env_file) if args.env_file else None,
                       Path(args.key_root) if args.key_root else None,
                       args.secret, operators(args.operators),
                       [tuple(m.split("=", 1)) for m in args.map])
        if args.strict_pending and report["pending"]:
            report["ok"] = False
    except Stop as exc:
        print(json.dumps({"error": str(exc), "kind": "Stop"}))
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
