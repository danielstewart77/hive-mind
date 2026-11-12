"""Bring a database to the hive's schema, and say first whether it can be.

    python -m hive_data.migrate check --dsn D    # exit 0, or 2 with problems
    python -m hive_data.migrate apply --dsn D    # exit 0, 2 refused, 1 failed

`--dsn` defaults to `HIVE_DATABASE_URL`. Both print one JSON object on
stdout and, when something is wrong, one line per problem on stderr.

The schema is a numbered list of steps. The four baseline steps are the four
files in `schema/` — `core` (1), `memory` (2), `tools` (3), `board` (4), in
that order because `board.design_sessions` has foreign keys into `core`.
Every later fix is a new file `schema/steps/NNNN_name.sql` with NNNN above 4;
the baseline files are never edited to change a schema that has shipped,
because a hive that already ran them would never see the edit.

Each step runs in one transaction together with the row recording it in
`hive_meta.migrations`, so a step is either applied and recorded or neither.

A database the hive reached before this record existed is *adopted* rather
than re-run: a baseline step whose schema already holds every table this
package declares for it is recorded without running. "Present" means every
declared table, not the schema existing — `schema.verify` lets a database
open with no `memory` schema at all, which is right for comms and wrong for
deciding an install is done. A schema holding some of its tables is neither
present nor absent and is refused: the baseline files are bare `CREATE`s and
cannot complete one, and guessing which objects are missing is how a
migration damages a hive it was asked to finish.

`check` writes nothing. `apply` runs `check` first and writes nothing when it
reports a problem.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg

from .errors import MigrationFailed, MigrationRefused
from .schema import COLUMNS, bare, schema_of

_SCHEMA_DIR = Path(__file__).parent / "schema"
STEPS_DIR = _SCHEMA_DIR / "steps"

#: Where the record lives. Its own schema so that no hive schema has to
#: exist for the record to, and so that `public` — which on Postgres 15 and
#: later a non-owner may not create in — is never written.
RECORD_SCHEMA = "hive_meta"
RECORD = f"{RECORD_SCHEMA}.migrations"

#: One migration at a time per database. Two installers racing would each
#: see a step pending and the second would fail on objects the first made.
_LOCK = "hive_data.migrate"

#: The extensions the memory step creates, and whether a non-superuser may.
#: `vector` is not trusted: only a superuser installs it. `unaccent` is
#: trusted, which needs CREATE on the database instead.
EXTENSIONS: dict[str, bool] = {"vector": False, "unaccent": True}
_MEMORY_VERSION = 2


@dataclass(frozen=True)
class Step:
    version: int
    name: str
    sql: str
    #: The hive schema a baseline step creates. Only these can be adopted.
    schema: str | None = None


def baseline() -> list[Step]:
    """The four files in `schema/`, as steps 1 to 4."""
    return [
        Step(n, s, (_SCHEMA_DIR / f"{s}.sql").read_text(), schema=s)
        for n, s in ((1, "core"), (_MEMORY_VERSION, "memory"),
                     (3, "tools"), (4, "board"))
    ]


_STEP_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


def steps(directory: Path | None = None) -> list[Step]:
    """Every step this package ships, in version order."""
    out = baseline()
    folder = STEPS_DIR if directory is None else directory
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            if path.name.startswith("."):
                continue
            match = _STEP_FILE.match(path.name)
            if not match:
                raise ValueError(
                    f"{path.name} is not a step file: name it NNNN_name.sql")
            out.append(Step(int(match[1]), match[2], path.read_text()))
    versions = [s.version for s in out]
    if versions != sorted(set(versions)):
        raise ValueError(f"step versions must be unique and ordered: {versions}")
    return out


def _declared(schema: str) -> set[str]:
    return {bare(t) for t in COLUMNS if schema_of(t) == schema}


async def _recorded(connection) -> dict[int, str]:
    exists = await connection.fetchval(
        "SELECT to_regclass($1) IS NOT NULL", RECORD)
    if not exists:
        return {}
    rows = await connection.fetch(f"SELECT version, name FROM {RECORD}")
    return {r["version"]: r["name"] for r in rows}


async def _tables(connection, schema: str) -> set[str] | None:
    """The tables a schema holds, or None when the schema does not exist.

    Read from `pg_catalog`, not `information_schema`, which shows a role
    only the tables it holds a privilege on.
    """
    if not await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = $1)",
            schema):
        return None
    rows = await connection.fetch(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n "
        "ON n.oid = c.relnamespace WHERE n.nspname = $1 "
        "AND c.relkind IN ('r', 'p')", schema)
    return {r["relname"] for r in rows}


async def _plan(connection, chosen: list[Step]) -> dict[str, Any]:
    """What `apply` would do, and every reason it cannot. Reads only."""
    problems: list[str] = []
    recorded = await _recorded(connection)
    known = {s.version for s in chosen}
    for version in sorted(set(recorded) - known):
        problems.append(
            f"the database records step {version} ({recorded[version]}), "
            "which this hive_data does not know: it was migrated by a newer "
            "one. Upgrade hive_data rather than run an older schema on it")

    adopt: list[Step] = []
    run: list[Step] = []
    for step in chosen:
        if step.version in recorded:
            continue
        if step.schema is not None:
            have = await _tables(connection, step.schema)
            wanted = _declared(step.schema)
            if have is not None and wanted <= have:
                adopt.append(step)
                continue
            if have and have & wanted:
                problems.append(
                    f"the {step.schema} schema is partly there: it lacks "
                    + ", ".join(sorted(f"{step.schema}.{t}"
                                       for t in wanted - have))
                    + ". A partial schema is not completed automatically; "
                    "restore it or drop it so the step can create it whole")
                continue
        run.append(step)

    privileges = await connection.fetchrow(
        "SELECT r.rolsuper AS superuser, r.rolcreaterole AS createrole, "
        "has_database_privilege(current_database(), 'CREATE') AS db_create, "
        "current_user AS role, current_database() AS db "
        "FROM pg_roles r WHERE r.rolname = current_user")
    creates_schema = bool(adopt or run) and not await connection.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = $1)",
        RECORD_SCHEMA)
    creates_schema = creates_schema or any(
        s.schema or _CREATES_SCHEMA.search(_code(s.sql)) for s in run)
    if creates_schema and not privileges["db_create"]:
        problems.append(
            f"role {privileges['role']} has no CREATE on database "
            f"{privileges['db']}, and the schema needs it to create its "
            f"schemas. Run: GRANT CREATE ON DATABASE \"{privileges['db']}\" "
            f"TO \"{privileges['role']}\"")

    problems.extend(await _reference_problems(connection, run, privileges))
    problems.extend(await _role_problems(connection, run, privileges))

    if any(s.version == _MEMORY_VERSION for s in run):
        problems.extend(await _extension_problems(connection, privileges))

    return {"problems": problems, "recorded": sorted(recorded),
            "adopt": adopt, "run": run}


#: A later step that makes a schema of its own (step 6 makes `staff`) needs
#: CREATE on the database just as a baseline step does. Only the baseline
#: steps declare the schema they make, so the later ones are read for it.
_CREATES_SCHEMA = re.compile(r"\bCREATE\s+SCHEMA\b", re.IGNORECASE)

#: A foreign key into a table that already exists needs the REFERENCES
#: privilege on it, which a role that owns its own schema does not get on
#: someone else's (`core.sessions` is the usual target).
_REFERENCES = re.compile(
    r"\bREFERENCES\s+([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE)


#: A step that makes a role (step 7 makes `hive_staff`) needs CREATEROLE,
#: unless the role is already there and the step only grants to it.
_CREATES_ROLE = re.compile(
    r"\bCREATE\s+ROLE\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


async def _role_problems(connection, run: list[Step],
                         privileges) -> list[str]:
    if privileges["superuser"] or privileges["createrole"]:
        return []
    out: list[str] = []
    for step in run:
        for name in _CREATES_ROLE.findall(_code(step.sql)):
            if await connection.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_roles "
                    "WHERE rolname = $1)", name.lower()):
                continue
            out.append(
                f"step {step.version} ({step.name}) creates role "
                f"{name.lower()}, and role {privileges['role']} may not "
                f"create roles. Run as a superuser: CREATE ROLE "
                f"{name.lower()} NOLOGIN")
    return out


def _code(sql: str) -> str:
    """A step's SQL without its `--` comments, which name things freely."""
    return re.sub(r"--[^\n]*", "", sql)


async def _reference_problems(connection, run: list[Step],
                              privileges) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for step in run:
        for schema, table in _REFERENCES.findall(_code(step.sql)):
            target = f"{schema}.{table}".lower()
            if target in seen:
                continue
            seen.add(target)
            # A table not there yet is one these steps create, as this role.
            # Looked up in `pg_catalog` by name rather than `to_regclass`,
            # which raises for a role with no USAGE on the schema.
            row = await connection.fetchrow(
                "SELECT c.oid, has_table_privilege(c.oid, 'REFERENCES') "
                "AS refs, has_schema_privilege(n.oid, 'USAGE') AS usage "
                "FROM pg_class c JOIN pg_namespace n "
                "ON n.oid = c.relnamespace "
                "WHERE lower(n.nspname) = $1 AND lower(c.relname) = $2",
                schema.lower(), table.lower())
            if row is None:
                continue
            role = privileges["role"]
            lacks, grants = [], []
            if not row["usage"]:
                lacks.append(f"USAGE on schema {schema.lower()}")
                grants.append(f'GRANT USAGE ON SCHEMA {schema.lower()} '
                              f'TO "{role}"')
            if not row["refs"]:
                lacks.append(f"REFERENCES on {target}")
                grants.append(f'GRANT REFERENCES ON {target} TO "{role}"')
            if lacks:
                out.append(
                    f"step {step.version} ({step.name}) has a foreign key "
                    f"into {target}, and role {role} lacks "
                    + " and ".join(lacks) + ". Run: " + "; ".join(grants))
    return out


async def _extension_problems(connection, privileges) -> list[str]:
    out: list[str] = []
    public_create = await connection.fetchval(
        "SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_namespace "
        "WHERE nspname = 'public') "
        "THEN has_schema_privilege('public', 'CREATE') END")
    for name, trusted in EXTENSIONS.items():
        row = await connection.fetchrow(
            "SELECT e.extnamespace::regnamespace::text AS schema "
            "FROM pg_extension e WHERE e.extname = $1", name)
        if row is not None:
            if row["schema"] != "public":
                out.append(
                    f"extension {name} is installed in schema "
                    f"{row['schema']}; the memory schema expects it in "
                    f"public. Run: ALTER EXTENSION {name} SET SCHEMA public")
            continue
        available = await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_available_extensions "
            "WHERE name = $1)", name)
        if not available:
            package = "pgvector" if name == "vector" else "postgresql-contrib"
            out.append(
                f"extension {name} is not available on this server: install "
                f"{package} on the Postgres host (or use an image that has "
                "it, such as pgvector/pgvector)")
            continue
        if not privileges["superuser"]:
            if not trusted:
                out.append(
                    f"extension {name} is not installed and role "
                    f"{privileges['role']} may not add it: {name} is not a "
                    "trusted extension, so a superuser must run "
                    f"CREATE EXTENSION {name} WITH SCHEMA public on database "
                    f"{privileges['db']} first")
                continue
            if not privileges["db_create"]:
                out.append(
                    f"extension {name} is not installed and role "
                    f"{privileges['role']} may not add it without CREATE on "
                    f"database {privileges['db']}")
                continue
        if public_create is None:
            out.append(f"extension {name} goes in schema public, which "
                       "this database does not have")
        elif not public_create:
            out.append(
                f"extension {name} is not installed and role "
                f"{privileges['role']} has no CREATE on schema public, where "
                f"it goes. Run: GRANT CREATE ON SCHEMA public TO "
                f"\"{privileges['role']}\" (or create the extension as its "
                "owner)")
    return out


def _report(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": not plan["problems"],
        "problems": plan["problems"],
        "recorded": plan["recorded"],
        "adopt": [s.version for s in plan["adopt"]],
        "pending": [{"version": s.version, "name": s.name}
                    for s in plan["run"]],
    }


async def check(dsn: str, *, chosen: list[Step] | None = None
                ) -> dict[str, Any]:
    """Whether `apply` can bring this database to the schema. Writes nothing.

    `{"ok", "problems": [str], "recorded": [int], "adopt": [int],
    "pending": [{"version", "name"}]}`.
    """
    connection = await asyncpg.connect(dsn)
    try:
        async with connection.transaction(readonly=True):
            return _report(await _plan(connection, chosen or steps()))
    finally:
        await connection.close()


async def apply(dsn: str, *, chosen: list[Step] | None = None
                ) -> dict[str, Any]:
    """Bring the database to every step; refuse before writing if it cannot.

    Returns `{"ok": True, "adopted": [int], "applied": [int],
    "version": int}`. Raises `MigrationRefused` (nothing written) or
    `MigrationFailed` (that step left nothing; earlier ones stay recorded).
    """
    chosen = chosen or steps()
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(
            "SELECT pg_advisory_lock(hashtextextended($1, 0))", _LOCK)
        plan = await _plan(connection, chosen)
        if plan["problems"]:
            raise MigrationRefused(plan["problems"])
        adopted: list[int] = []
        applied: list[int] = []
        if plan["adopt"] or plan["run"]:
            async with connection.transaction():
                await connection.execute(
                    f"CREATE SCHEMA IF NOT EXISTS {RECORD_SCHEMA}; "
                    f"CREATE TABLE IF NOT EXISTS {RECORD} ("
                    "version integer PRIMARY KEY, name text NOT NULL, "
                    "how text NOT NULL CHECK (how IN ('ran', 'adopted')), "
                    "applied_at timestamptz NOT NULL DEFAULT now())")
                for step in plan["adopt"]:
                    await _record(connection, step, "adopted")
                    adopted.append(step.version)
        for step in plan["run"]:
            try:
                async with connection.transaction():
                    await connection.execute(step.sql)
                    await _record(connection, step, "ran")
            except asyncpg.PostgresError as error:
                raise MigrationFailed(step.version, step.name,
                                      f"{type(error).__name__}: {error}",
                                      applied) from error
            applied.append(step.version)
        version = await connection.fetchval(f"SELECT max(version) FROM {RECORD}")
        return {"ok": True, "adopted": adopted, "applied": applied,
                "version": version}
    finally:
        await connection.close()


async def _record(connection, step: Step, how: str) -> None:
    # Qualified: every baseline file sets `search_path` to '' for the
    # session.
    await connection.execute(
        f"INSERT INTO {RECORD} (version, name, how) VALUES ($1, $2, $3)",
        step.version, step.name, how)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hive_data.migrate")
    parser.add_argument("command", choices=("check", "apply"))
    parser.add_argument("--dsn", default=os.environ.get("HIVE_DATABASE_URL"),
                        help="defaults to HIVE_DATABASE_URL")
    args = parser.parse_args(argv)
    if not args.dsn:
        parser.error("--dsn is required when HIVE_DATABASE_URL is unset")

    def emit(out: dict[str, Any], problems: list[str]) -> None:
        print(json.dumps(out))
        for problem in problems:
            print(problem, file=sys.stderr)

    try:
        if args.command == "check":
            out = asyncio.run(check(args.dsn))
            emit(out, out["problems"])
            return 0 if out["ok"] else 2
        out = asyncio.run(apply(args.dsn))
        emit(out, [])
        return 0
    except MigrationRefused as refused:
        emit({"ok": False, "problems": refused.problems}, refused.problems)
        return 2
    except MigrationFailed as failed:
        emit({"ok": False, "failed": {"version": failed.version,
                                      "name": failed.name,
                                      "error": failed.error},
              "applied": failed.applied}, [str(failed)])
        return 1
    except (OSError, asyncpg.PostgresError) as error:
        # Unreachable, refused credentials, no such database: nothing was
        # checked, so this is a problem in the same shape as the others.
        problem = f"cannot reach the database: {type(error).__name__}: {error}"
        emit({"ok": False, "problems": [problem]}, [problem])
        return 2


if __name__ == "__main__":
    sys.exit(main())
