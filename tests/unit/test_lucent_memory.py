"""Unit tests for Lucent memory module -- vector store operations via SQLite."""

import json
import sqlite3
from unittest.mock import patch

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
        # Hash-based deterministic embedding for testing
        h = hash(text)
        rng = np.random.RandomState(abs(h) % (2**31))
        return rng.randn(dim).tolist()

    return patch("tools.stateful.lucent_memory._embed", side_effect=fake_embed)


# ---------------------------------------------------------------------------
# memory_store_direct tests
# ---------------------------------------------------------------------------
class TestMemoryStoreDirect:
    """Tests for memory_store_direct."""

    def test_inserts_row(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn), _mock_embed():
            result = json.loads(lm.memory_store_direct(
                content="Test memory",
                data_class="person",
                agent_id="ada",
                source="user",
            ))
        assert result["stored"] is True
        assert "id" in result
        assert result["agent_id"] == "ada"
        assert result["data_class"] == "person"

    def test_stores_embedding_as_blob(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn), _mock_embed():
            lm.memory_store_direct(
                content="Test memory",
                data_class="person",
                agent_id="ada",
                source="user",
            )
        row = conn.execute("SELECT embedding FROM memories").fetchone()
        assert row["embedding"] is not None
        # Should be decodable as float32 numpy array
        arr = np.frombuffer(row["embedding"], dtype=np.float32)
        assert len(arr) == 4096

    def test_invalid_source(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn), _mock_embed():
            result = json.loads(lm.memory_store_direct(
                content="Test", data_class="person", agent_id="ada", source="random",
            ))
        assert result["stored"] is False

    def test_invalid_data_class(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn), _mock_embed():
            result = json.loads(lm.memory_store_direct(
                content="Test", data_class="unknown-class", agent_id="ada", source="user",
            ))
        assert result["stored"] is False

    def test_calls_embed(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn), patch.object(lm, "_embed", return_value=[0.1] * 4096) as mock_e:
            lm.memory_store_direct(
                content="Test content", data_class="person", agent_id="ada", source="user",
            )
        mock_e.assert_called_once_with("Test content")

    def test_store_delegates_to_store_direct(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn), _mock_embed():
            result = json.loads(lm.memory_store(
                content="Test memory", data_class="person", agent_id="ada", source="user",
            ))
        assert result["stored"] is True


# ---------------------------------------------------------------------------
# memory_list tests
# ---------------------------------------------------------------------------
class TestMemoryList:
    """Tests for memory_list."""

    def test_returns_paginated(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn), _mock_embed():
            for i in range(5):
                lm.memory_store_direct(
                    content=f"Memory {i}", data_class="person",
                    agent_id="ada", source="user",
                )
            result = json.loads(lm.memory_list(offset=1, limit=2, agent_id="ada"))
        assert result["total"] == 5
        assert result["offset"] == 1
        assert result["limit"] == 2
        assert len(result["entries"]) == 2

    def test_respects_max_limit(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn), _mock_embed():
            result = json.loads(lm.memory_list(limit=200, agent_id="ada"))
        assert result["limit"] == 100

    def test_empty(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn):
            result = json.loads(lm.memory_list(agent_id="ada"))
        assert result["total"] == 0
        assert result["entries"] == []


# ---------------------------------------------------------------------------
# memory_delete tests
# ---------------------------------------------------------------------------
class TestMemoryDelete:
    """Tests for memory_delete."""

    def test_removes_row(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn), _mock_embed():
            store_result = json.loads(lm.memory_store_direct(
                content="To delete", data_class="person", agent_id="ada", source="user",
            ))
            mem_id = str(store_result["id"])
            result = json.loads(lm.memory_delete(mem_id))
        assert result["deleted"] is True
        assert result["content"] == "To delete"

    def test_not_found(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn):
            result = json.loads(lm.memory_delete("99999"))
        assert result["deleted"] is False


# ---------------------------------------------------------------------------
# memory_update tests
# ---------------------------------------------------------------------------
class TestMemoryUpdate:
    """Tests for memory_update."""

    def test_content_reembeds(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn), _mock_embed():
            store = json.loads(lm.memory_store_direct(
                content="Old content", data_class="person", agent_id="ada", source="user",
            ))
            mem_id = str(store["id"])

        with _patch_conn(conn), patch.object(lm, "_embed", return_value=[0.5] * 4096) as mock_e:
            result = json.loads(lm.memory_update(mem_id, content="New content"))
        assert result["updated"] is True
        mock_e.assert_called_once_with("New content")

    def test_update_data_class(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn), _mock_embed():
            store = json.loads(lm.memory_store_direct(
                content="Test", data_class="person", agent_id="ada", source="user",
            ))
            mem_id = str(store["id"])
            result = json.loads(lm.memory_update(mem_id, data_class="preference"))
        assert result["updated"] is True
        assert result["data_class"] == "preference"

    def test_update_tags(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn), _mock_embed():
            store = json.loads(lm.memory_store_direct(
                content="Test", data_class="person", agent_id="ada", source="user",
            ))
            mem_id = str(store["id"])
            result = json.loads(lm.memory_update(mem_id, tags="newtag1,newtag2"))
        assert result["updated"] is True

    def test_no_fields_error(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn):
            result = json.loads(lm.memory_update("1"))
        assert result["updated"] is False
        assert "no fields" in result["error"]

    def test_not_found(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn):
            result = json.loads(lm.memory_update("99999", content="X"))
        assert result["updated"] is False


# ---------------------------------------------------------------------------
# memory_retrieve tests
# ---------------------------------------------------------------------------
class TestMemoryRetrieve:
    """Tests for memory_retrieve (cosine similarity)."""

    def test_cosine_similarity_ordering(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn), _mock_embed():
            lm.memory_store_direct(
                content="Python programming", data_class="person",
                agent_id="ada", source="user",
            )
            lm.memory_store_direct(
                content="JavaScript programming", data_class="person",
                agent_id="ada", source="user",
            )
            result = json.loads(lm.memory_retrieve(
                query="Python code", k=2, agent_id="ada",
            ))
        assert result["count"] == 2
        # Scores should be in descending order
        scores = [m["score"] for m in result["memories"]]
        assert scores == sorted(scores, reverse=True)

    def test_with_tag_filter(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn), _mock_embed():
            lm.memory_store_direct(
                content="Tagged memory", data_class="person",
                agent_id="ada", source="user", tags="special",
            )
            lm.memory_store_direct(
                content="Untagged memory", data_class="person",
                agent_id="ada", source="user", tags="other",
            )
            result = json.loads(lm.memory_retrieve(
                query="memory", k=10, agent_id="ada", tag_filter="special",
            ))
        assert result["count"] == 1
        assert result["memories"][0]["tags"] == "special"

    def test_respects_agent_id(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn), _mock_embed():
            lm.memory_store_direct(
                content="Ada memory", data_class="person", agent_id="ada", source="user",
            )
            lm.memory_store_direct(
                content="Bob memory", data_class="person", agent_id="bob", source="user",
            )
            result = json.loads(lm.memory_retrieve(
                query="memory", k=10, agent_id="ada",
            ))
        assert result["count"] == 1
        assert result["memories"][0]["agent_id"] == "ada"

    def test_max_k_capped(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn), _mock_embed():
            for i in range(55):
                lm.memory_store_direct(
                    content=f"Memory {i}", data_class="person",
                    agent_id="ada", source="user",
                )
            result = json.loads(lm.memory_retrieve(
                query="memory", k=100, agent_id="ada",
            ))
        assert result["count"] <= 50

    def test_empty(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn), _mock_embed():
            result = json.loads(lm.memory_retrieve(
                query="nothing", k=10, agent_id="ada",
            ))
        assert result["count"] == 0
        assert result["memories"] == []

    def test_returns_all_metadata_fields(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with _patch_conn(conn), _mock_embed():
            lm.memory_store_direct(
                content="Full metadata test", data_class="person",
                agent_id="ada", source="user",
            )
            result = json.loads(lm.memory_retrieve(
                query="Full metadata", k=1, agent_id="ada",
            ))
        assert result["count"] == 1
        mem = result["memories"][0]
        expected_keys = {
            "content", "tags", "source", "agent_id", "created_at", "score",
            "data_class", "tier", "as_of", "expires_at", "superseded", "codebase_ref",
        }
        assert expected_keys.issubset(set(mem.keys()))


# ---------------------------------------------------------------------------
# MEMORY_TOOLS list test
# ---------------------------------------------------------------------------
class TestMemoryToolsList:
    """Test that MEMORY_TOOLS contains the expected functions."""

    def test_memory_tools_list_matches_original(self):
        import tools.stateful.lucent_memory as lm

        expected_names = {
            "memory_store", "memory_store_direct", "memory_list",
            "memory_delete", "memory_update", "memory_retrieve",
        }
        actual_names = {f.__name__ for f in lm.MEMORY_TOOLS}
        assert expected_names == actual_names
