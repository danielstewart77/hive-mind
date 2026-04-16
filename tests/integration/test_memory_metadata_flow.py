"""Integration tests for memory metadata flow -- store and retrieve with metadata.

Updated for Lucent (SQLite) backend.
"""

import json
import sqlite3
from unittest.mock import patch

import numpy as np
import pytest


def _make_test_conn() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with Lucent schema."""
    import tools.stateful.lucent as lucent_mod

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    lucent_mod._init_schema(conn)
    return conn


def _patch_conn(conn):
    return patch("tools.stateful.lucent._get_connection", return_value=conn)


def _mock_embed():
    def fake_embed(text: str) -> list[float]:
        h = hash(text)
        rng = np.random.RandomState(abs(h) % (2**31))
        return rng.randn(4096).tolist()

    return patch("tools.stateful.lucent_memory._embed", side_effect=fake_embed)


class TestStoreRetrieveMetadataFlow:
    """Tests for the full store-then-retrieve flow with metadata."""

    def test_store_then_retrieve_preserves_metadata(self) -> None:
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        # Store with data_class
        with _patch_conn(conn), _mock_embed():
            store_result_str = lm.memory_store_direct(
                content="Daniel prefers dark mode",
                tags="preference",
                source="user",
                data_class="preference",
            )
            store_result = json.loads(store_result_str)
            assert store_result["stored"] is True
            assert store_result["data_class"] == "preference"

            # Verify stored data
            row = conn.execute(
                "SELECT data_class, tier FROM memories WHERE id = ?",
                (store_result["id"],),
            ).fetchone()
            assert row["data_class"] == "preference"
            assert row["tier"] == "durable"

        # Retrieve and verify metadata is included
        with _patch_conn(conn), _mock_embed():
            retrieve_result_str = lm.memory_retrieve(query="dark mode preference")
            retrieve_result = json.loads(retrieve_result_str)
            assert retrieve_result["count"] == 1
            memory = retrieve_result["memories"][0]
            assert memory["data_class"] == "preference"
            assert memory["tier"] == "durable"
