"""Regression test for the `data_class` filter on memory_list.

Bug: the underlying `memory_list` function silently ignored any
`data_class` argument, so callers using it as a delete scope
(`list filtered-rows → DELETE each id`) deleted the entire table.
This test pins that `data_class=<x>` returns ONLY rows whose
data_class equals `<x>`, and that omitting the argument returns
all rows.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest


class MemoryListDataClassFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "lucent.db")

        from lucent_api import lucent as lucent_mod
        from lucent_api import lucent_memory as mem_mod

        self._lucent_mod = lucent_mod
        self._mem_mod = mem_mod
        self._orig_db_path = lucent_mod.DB_PATH

        lucent_mod.DB_PATH = self.db_path
        # Reset per-thread connection state so the test gets a fresh DB.
        lucent_mod._thread_local = type(lucent_mod._thread_local)()
        lucent_mod._schema_initialized = False

        conn = lucent_mod._get_connection()
        # Seed three rows across three data classes.
        now = 1700000000
        conn.executemany(
            """INSERT INTO memories
               (content, tags, source, data_class, tier, mind_id,
                created_at, expires_at, recurring, codebase_ref, embedding)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("ephemeral row", "", "session", "ephemeral", "contextual",
                 "m1", now + 1, None, 0, None, None),
                ("feedback row", "", "session", "feedback", "contextual",
                 "m1", now + 2, None, 0, None, None),
                ("person row", "", "session", "person", "contextual",
                 "m1", now + 3, None, 0, None, None),
            ],
        )
        conn.commit()

    def tearDown(self) -> None:
        try:
            conn = getattr(self._lucent_mod._thread_local, "conn", None)
            if conn is not None:
                conn.close()
        finally:
            # Reset module state so the next test starts clean.
            self._lucent_mod._thread_local = type(self._lucent_mod._thread_local)()
            self._lucent_mod._schema_initialized = False
            self._lucent_mod.DB_PATH = self._orig_db_path
            self.tmp_dir.cleanup()

    def test_filter_returns_only_matching_data_class(self) -> None:
        result = json.loads(self._mem_mod.memory_list(data_class="ephemeral"))
        self.assertEqual(result["total"], 1, result)
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["entries"][0]["data_class"], "ephemeral")

    def test_unfiltered_returns_all_rows(self) -> None:
        result = json.loads(self._mem_mod.memory_list())
        self.assertEqual(result["total"], 3, result)

    def test_filter_with_no_matches_returns_zero(self) -> None:
        result = json.loads(self._mem_mod.memory_list(data_class="nope"))
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["entries"], [])


if __name__ == "__main__":
    unittest.main()
