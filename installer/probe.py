"""What a database already holds, read without writing a thing.

Run inside the hive network (it is the script `gather_script()` hands to the
comms image, which has asyncpg) or imported where asyncpg is installed. The
answer decides two things before anything is written: whether this is a hive
already (and so whether the admin must be an existing account and which
names are taken), and whether the proxy may create its own schema.
"""

from __future__ import annotations

import inspect
import json

QUERIES = {
    "users": "SELECT username FROM core.users WHERE disabled_at IS NULL",
    "minds": "SELECT mind_id, name FROM core.minds",
    "comms_holders": "SELECT holder FROM core.credentials",
    "lucent_holders": "SELECT holder FROM memory.credentials",
}


async def gather(dsn: str, timeout: float = 10) -> dict:
    """{"tables": {...present}, "users", "minds", "comms_holders",
    "lucent_holders", "proxy_migrated"} — read-only, in a read-only
    transaction."""
    import asyncpg

    conn = await asyncpg.connect(dsn, timeout=timeout)
    try:
        async with conn.transaction(readonly=True):
            present = {
                f"{r['table_schema']}.{r['table_name']}"
                for r in await conn.fetch(
                    "SELECT table_schema, table_name FROM information_schema.tables "
                    "WHERE table_schema IN ('core', 'memory', 'proxy')")
            }
            out: dict = {"tables": sorted(present)}
            for key, sql in QUERIES.items():
                table = sql.split(" FROM ", 1)[1].split()[0]
                rows = await conn.fetch(sql) if table in present else []
                out[key] = [dict(r) if len(r) > 1 else r[0] for r in rows]
            out["proxy_migrated"] = "proxy.alembic_version" in present
            return out
    finally:
        await conn.close()


def gather_script() -> str:
    """This module as a script for `Shell.in_network`, which defines PAYLOAD."""
    return ("import asyncio\nQUERIES = " + json.dumps(QUERIES) + "\n"
            + inspect.getsource(gather)
            + "try:\n    print(json.dumps(asyncio.run(gather(PAYLOAD['dsn']))))\n"
            + "except Exception as exc:\n"
            + "    print(json.dumps({'error': f'{type(exc).__name__}: {exc}'}))\n")
