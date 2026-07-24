"""One-shot rename: ``agent_id`` column → ``mind_id`` on lucent.db.

Renames the column on ``nodes``, ``edges``, and ``memories``. SQLite's
``ALTER TABLE ... RENAME COLUMN`` automatically updates any indexes that
reference the renamed column, but the index NAMES still reflect the old
column name. We drop the old-named indexes here so that on next container
start, ``lucent_api/lucent.py`` recreates them under the new names
(``idx_*_mind_id``) via ``CREATE INDEX IF NOT EXISTS``.

Prerequisites:
  - ``hive-lucent`` container is stopped (otherwise concurrent access can
    corrupt). After this script returns, rebuild and restart the container
    with the new ``lucent_api/`` code.

Run from /home/USER/Storage/Dev/hive_mind/nervous-system/.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path("/home/USER/Storage/Dev/hive_mind/nervous-system/data/lucent.db")
TABLES = ("nodes", "edges", "memories")
OLD_INDEX_NAMES = (
    "idx_nodes_agent_id",
    "idx_edges_agent_id",
    "idx_memories_agent_id",
)


def _table_has_column(con: sqlite3.Connection, table: str, column: str) -> bool:
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def main() -> int:
    if not DB_PATH.is_file():
        sys.exit(f"lucent.db not found at {DB_PATH}")

    con = sqlite3.connect(str(DB_PATH))
    try:
        con.execute("BEGIN")
        for tbl in TABLES:
            if _table_has_column(con, tbl, "agent_id"):
                if _table_has_column(con, tbl, "mind_id"):
                    sys.exit(
                        f"{tbl}: both agent_id AND mind_id columns exist — "
                        f"refusing to proceed, inspect manually"
                    )
                con.execute(f"ALTER TABLE {tbl} RENAME COLUMN agent_id TO mind_id")
                print(f"renamed {tbl}.agent_id → {tbl}.mind_id")
            elif _table_has_column(con, tbl, "mind_id"):
                print(f"{tbl}: already mind_id — skip")
            else:
                sys.exit(f"{tbl}: neither agent_id nor mind_id — schema unexpected")

        for idx in OLD_INDEX_NAMES:
            con.execute(f"DROP INDEX IF EXISTS {idx}")
            print(f"dropped (if existed) {idx}")

        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()

    # Verify
    con = sqlite3.connect(str(DB_PATH))
    try:
        for tbl in TABLES:
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({tbl})")]
            assert "mind_id" in cols, f"{tbl} missing mind_id post-migration"
            assert "agent_id" not in cols, f"{tbl} still has agent_id post-migration"
        print("verification OK")
    finally:
        con.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
