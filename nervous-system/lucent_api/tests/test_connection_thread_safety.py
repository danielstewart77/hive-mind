"""Thread-safety regression for ``lucent._get_connection``.

Bug: lucent used a single shared sqlite3 Connection with
check_same_thread=False. Under concurrent reads from uvicorn's
threadpool, two threads racing the same cursor produced
``sqlite3.InterfaceError: bad parameter or other API misuse``.
Fix: per-thread connection via threading.local, schema init guarded
by a one-shot lock. This test pins the new contract.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest


class ConnectionThreadSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "lucent.db")

        from lucent_api import lucent as lucent_mod
        from lucent_api import lucent_memory as mem_mod

        self._lucent_mod = lucent_mod
        self._mem_mod = mem_mod
        self._orig_db_path = lucent_mod.DB_PATH

        lucent_mod.DB_PATH = self.db_path
        lucent_mod._thread_local = type(lucent_mod._thread_local)()
        lucent_mod._schema_initialized = False

        # Seed a row from the main thread so memory_list has work to do.
        conn = lucent_mod._get_connection()
        conn.execute(
            """INSERT INTO memories
               (content, tags, source, data_class, tier, mind_id,
                created_at, expires_at, recurring, codebase_ref, embedding)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("seed", "", "session", "feedback", "contextual",
             "m1", 1700000000, None, 0, None, None),
        )
        conn.commit()

    def tearDown(self) -> None:
        # Reset module state.
        self._lucent_mod._thread_local = type(self._lucent_mod._thread_local)()
        self._lucent_mod._schema_initialized = False
        self._lucent_mod.DB_PATH = self._orig_db_path
        self.tmp_dir.cleanup()

    def test_each_thread_gets_its_own_connection(self) -> None:
        """Two threads calling _get_connection get different Connection objects."""
        connections: dict[int, sqlite3.Connection] = {}

        def worker(tid: int) -> None:
            connections[tid] = self._lucent_mod._get_connection()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        ids = {id(c) for c in connections.values()}
        self.assertEqual(len(ids), 4, "Each thread must get a distinct connection")

    def test_concurrent_memory_list_does_not_raise(self) -> None:
        """Hammering memory_list from many threads must not raise InterfaceError.

        This is the exact bug we hit in production: two simultaneous reads
        through the shared singleton corrupted cursor state.
        """
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def worker() -> None:
            try:
                barrier.wait(timeout=5)
                for _ in range(50):
                    payload = self._mem_mod.memory_list(limit=10)
                    result = json.loads(payload)
                    # Sanity: schema seeded one row, should always be visible.
                    self.assertGreaterEqual(result["total"], 1)
            except BaseException as exc:  # noqa: BLE001 - capture for assertion
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(
            errors, [],
            f"Concurrent memory_list raised: {[repr(e) for e in errors]}",
        )

    def test_schema_initialized_only_once(self) -> None:
        """Schema init must be idempotent across threads via the one-shot lock."""
        # Drop a row that would only exist after init runs once.
        conn = self._lucent_mod._get_connection()
        before = conn.execute("SELECT COUNT(*) FROM schema_types").fetchone()[0]

        # Force several more threads to call _get_connection.
        def worker() -> None:
            self._lucent_mod._get_connection()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        after = conn.execute("SELECT COUNT(*) FROM schema_types").fetchone()[0]
        self.assertEqual(before, after,
                         "schema_types must not be re-seeded by subsequent threads")


if __name__ == "__main__":
    unittest.main()
