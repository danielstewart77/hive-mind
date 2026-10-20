"""The one-time pass that gives every row already on disk an owner.

Every other test in this suite starts from an empty in-memory database, where
the column arrives with `CREATE TABLE` and the backfill sees no rows — so the
half that actually runs on deploy, against a store holding a hundred nodes
written before owners existed, was the half with nothing behind it.

These drive the real `_migrate_graph_owner` against a file built with the
pre-change schema.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lucent_api import holder_access as ha

OLD_NODES_DDL = """
CREATE TABLE nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mind_id     TEXT    NOT NULL,
    type        TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    first_name  TEXT,
    last_name   TEXT,
    properties  TEXT    DEFAULT '{}',
    data_class  TEXT,
    tier        TEXT,
    source      TEXT,
    as_of       TEXT,
    created_at  REAL,
    updated_at  REAL,
    UNIQUE(mind_id, name)
);
CREATE TABLE edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mind_id     TEXT    NOT NULL,
    source_id   INTEGER NOT NULL,
    target_id   INTEGER NOT NULL,
    type        TEXT    NOT NULL,
    as_of       TEXT,
    source      TEXT,
    data_class  TEXT,
    tier        TEXT,
    created_at  REAL,
    properties  TEXT    NOT NULL DEFAULT '{}'
);
"""

SKIPPY_UUID = "14cb820b-4a42-4f04-a593-54f532fd1d2f"
ZACH_UUID = "82cd16ef-f33e-4a90-9fbc-d24427b96680"


class GraphOwnerMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp()
        self.path = str(Path(self.dir) / "lucent.db")
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(OLD_NODES_DDL)
        ha.init_credentials(self.conn)
        # Skippy's credential exists; Zach's has not been minted here yet,
        # which is the live state for both of the boys' minds.
        ha.put_credential(self.conn, "skippy", "daniel", "t-skippy",
                          role=ha.MIND, mind_id=SKIPPY_UUID)

    def tearDown(self) -> None:
        self.conn.close()

    def _node(self, name: str, mind_id: str, node_type: str = "Person") -> int:
        cur = self.conn.execute(
            "INSERT INTO nodes (mind_id, type, name, created_at) VALUES (?, ?, ?, 1.0)",
            (mind_id, node_type, name),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _migrate(self, primary: str | None = "daniel") -> None:
        from lucent_api.lucent import _migrate_graph_owner

        env = {} if primary is None else {"LUCENT_PRIMARY_USER": primary}
        with mock.patch.dict(os.environ, env, clear=False):
            if primary is None:
                os.environ.pop("LUCENT_PRIMARY_USER", None)
            _migrate_graph_owner(self.conn)

    def _owner_of(self, name: str) -> object:
        return self.conn.execute(
            "SELECT owner FROM nodes WHERE name = ?", (name,)
        ).fetchone()["owner"]

    def test_a_row_takes_the_owner_of_the_mind_that_wrote_it(self):
        self._node("Maurice", SKIPPY_UUID)

        self._migrate()

        self.assertEqual(self._owner_of("Maurice"), "daniel")

    def test_a_row_whose_writer_is_unknown_takes_the_primary_user(self):
        """The console's provenance-less edges, and anything written before
        the credentials table existed."""
        self._node("Jose Portillo", "")

        self._migrate()

        self.assertEqual(self._owner_of("Jose Portillo"), "daniel")

    def test_a_minds_own_node_is_never_handed_to_the_primary_user(self):
        """Zach's credential has not been minted here, so his identity node
        has no resolvable owner. Giving it to Daniel takes that mind's soul
        away from the person it belongs to and then refuses the mind sight
        of its own node."""
        self._node("Zach", ZACH_UUID, node_type="Mind")

        self._migrate()

        self.assertIsNone(self._owner_of("Zach"))

    def test_a_minds_own_node_is_claimed_once_its_credential_exists(self):
        self._node("Zach", ZACH_UUID, node_type="Mind")
        self._migrate()

        ha.put_credential(self.conn, "zach", "aiden", "t-zach",
                          role=ha.MIND, mind_id=ZACH_UUID)
        self._migrate()

        self.assertEqual(self._owner_of("Zach"), "aiden")

    def test_with_no_primary_user_declared_a_row_is_left_ownerless(self):
        self._node("Jose Portillo", "")

        self._migrate(primary=None)

        self.assertIsNone(self._owner_of("Jose Portillo"))

    def test_an_edge_takes_the_owner_of_the_mind_that_wrote_it(self):
        one = self._node("Maurice", SKIPPY_UUID)
        two = self._node("Propel", SKIPPY_UUID, node_type="Organization")
        self.conn.execute(
            "INSERT INTO edges (mind_id, source_id, target_id, type, created_at) "
            "VALUES (?, ?, ?, 'MEMBER_OF', 1.0)",
            (SKIPPY_UUID, one, two),
        )
        self.conn.commit()

        self._migrate()

        self.assertEqual(
            [r["owner"] for r in self.conn.execute("SELECT owner FROM edges")],
            ["daniel"],
        )

    def test_the_pass_keys_nodes_on_the_owner_and_runs_twice_unchanged(self):
        self._node("Maurice", SKIPPY_UUID)

        self._migrate()
        self._migrate()

        self.assertEqual(self._owner_of("Maurice"), "daniel")
        indexes = {
            r["name"]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        self.assertIn("nodes_owner_type_name", indexes)

    def test_two_of_one_owners_minds_may_hold_one_name_of_two_types(self):
        """The old key was (mind_id, name) and ignored type, so collapsing
        the writers out of it would have made the second one an integrity
        error reported as a success."""
        self._node("Hive Mind", SKIPPY_UUID, node_type="Organization")
        self._node("Hive Mind", "mordecai-uuid", node_type="Place")

        self._migrate()

        rows = self.conn.execute(
            "SELECT type, owner FROM nodes WHERE name = 'Hive Mind' ORDER BY type"
        ).fetchall()
        self.assertEqual([r["type"] for r in rows], ["Organization", "Place"])

    def test_rows_that_collide_leave_the_store_serving(self):
        """Two rows that would break the new key. The index is skipped and
        reported; raising here runs inside schema init, which would leave the
        store answering nothing at all to anybody for the sake of a
        constraint it ran fine without the day before."""
        self._node("Maurice", SKIPPY_UUID)
        self._node("Maurice", "mordecai-uuid")

        self._migrate()

        self.assertEqual(self._owner_of("Maurice"), "daniel")
        indexes = {
            r["name"]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        self.assertNotIn("nodes_owner_type_name", indexes)


if __name__ == "__main__":
    unittest.main()
