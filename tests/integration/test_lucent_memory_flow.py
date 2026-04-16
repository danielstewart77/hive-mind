"""Integration tests for Lucent memory -- store and retrieve round trips."""

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


def _mock_embed(dim: int = 4096):
    """Return a patch that replaces _embed with a deterministic embedding."""
    def fake_embed(text: str) -> list[float]:
        h = hash(text)
        rng = np.random.RandomState(abs(h) % (2**31))
        return rng.randn(dim).tolist()

    return patch("tools.stateful.lucent_memory._embed", side_effect=fake_embed)


class TestStoreRetrieveRoundTrip:
    """Store a memory then retrieve it."""

    def test_store_then_retrieve_round_trip(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn), _mock_embed():
            store = json.loads(lm.memory_store_direct(
                content="Daniel likes dark mode",
                data_class="preference",
                agent_id="ada",
                source="user",
                tags="preference",
            ))
            assert store["stored"] is True

            retrieve = json.loads(lm.memory_retrieve(
                query="dark mode preference",
                k=5,
                agent_id="ada",
            ))
        assert retrieve["count"] >= 1
        assert retrieve["memories"][0]["content"] == "Daniel likes dark mode"
        assert retrieve["memories"][0]["data_class"] == "preference"
        assert retrieve["memories"][0]["tags"] == "preference"


class TestStoreMultipleRetrieveTopK:
    """Store multiple memories and retrieve top-k."""

    def test_store_multiple_retrieve_top_k(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn), _mock_embed():
            for i in range(5):
                lm.memory_store_direct(
                    content=f"Memory item number {i} about various topics",
                    data_class="person",
                    agent_id="ada",
                    source="user",
                )

            retrieve = json.loads(lm.memory_retrieve(
                query="Memory item number 0",
                k=3,
                agent_id="ada",
            ))
        assert retrieve["count"] == 3
        # First result should be the closest match
        scores = [m["score"] for m in retrieve["memories"]]
        assert scores == sorted(scores, reverse=True)
