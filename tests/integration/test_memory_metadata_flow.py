"""Integration tests for memory metadata flow -- store and retrieve with metadata."""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_neo4j_and_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure agents.knowledge_graph can be imported by mocking neo4j and agent_tooling."""
    if "neo4j" not in sys.modules:
        neo4j_mock = MagicMock()
        monkeypatch.setitem(sys.modules, "neo4j", neo4j_mock)
    if "agent_tooling" not in sys.modules:
        at_mock = MagicMock()
        at_mock.tool = MagicMock(return_value=lambda f: f)
        monkeypatch.setitem(sys.modules, "agent_tooling", at_mock)


def _make_mock_driver() -> MagicMock:
    """Create a mock Neo4j driver."""
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
    mock_result = MagicMock()
    mock_result.single.return_value = {"id": "test-id"}
    mock_session.run.return_value = mock_result
    return mock_driver


class TestStoreRetrieveMetadataFlow:
    """Tests for the full store-then-retrieve flow with metadata."""

    def test_store_then_retrieve_preserves_metadata(self) -> None:
        import agents.memory as mem_mod

        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value

        # Store with data_class
        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            store_result_str = mem_mod.memory_store_direct(
                content="Daniel prefers dark mode",
                tags="preference",
                source="user",
                data_class="preference",
            )
            store_result = json.loads(store_result_str)
            assert store_result["stored"] is True
            assert store_result["data_class"] == "preference"

            # Verify stored params
            store_call = mock_session.run.call_args_list[-1]
            params = store_call[1]
            assert params["data_class"] == "preference"
            assert params["tier"] == "durable"

        # Retrieve and verify metadata is included
        record_mock = MagicMock()
        record_mock.__getitem__ = lambda self, key: {
            "content": "Daniel prefers dark mode",
            "tags": "preference",
            "source": "user",
            "agent_id": "ada",
            "created_at": 1000,
            "score": 0.99,
            "data_class": "preference",
            "tier": "durable",
            "as_of": "2026-01-01T00:00:00Z",
            "expires_at": None,
            "superseded": False,
            "codebase_ref": None,
            "archived": None,
        }[key]
        retrieve_result_mock = MagicMock()
        retrieve_result_mock.__iter__ = MagicMock(return_value=iter([record_mock]))
        mock_session.run.return_value = retrieve_result_mock

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            retrieve_result_str = mem_mod.memory_retrieve(query="dark mode preference")
            retrieve_result = json.loads(retrieve_result_str)
            assert retrieve_result["count"] == 1
            memory = retrieve_result["memories"][0]
            assert memory["data_class"] == "preference"
            assert memory["tier"] == "durable"


class TestEpilogueWriteRequiresDataClass:
    """Tests that epilogue write_to_memory now passes data_class for all writes."""

    def test_epilogue_write_to_memory_passes_data_class(self) -> None:
        """Verify that write_to_memory passes data_class for all operations."""
        import asyncio
        import agents.memory as mem_mod
        import agents.knowledge_graph as kg_mod

        mem_calls: list[dict] = []
        kg_calls: list[dict] = []

        def capture_mem(**kwargs):
            mem_calls.append(kwargs)
            return json.dumps({"stored": True, "id": "test", "data_class": kwargs.get("data_class")})

        def capture_kg(**kwargs):
            kg_calls.append(kwargs)
            return json.dumps({"upserted": True, "id": "test"})

        digest = {
            "topics": ["Session covered dark mode preferences."],
            "entities": [
                {"name": "Daniel", "type": "person", "context": "Owner of Hive Mind"},
            ],
            "relationships": [],
        }

        with (
            patch("agents.memory.memory_store_direct", side_effect=capture_mem),
            patch("agents.knowledge_graph.graph_upsert_direct", side_effect=capture_kg),
        ):
            from core.epilogue import write_to_memory
            asyncio.run(write_to_memory(digest))

        # Memory store should have data_class="session-log"
        assert len(mem_calls) == 1
        assert mem_calls[0]["data_class"] == "session-log"
        assert mem_calls[0]["source"] == "session"

        # Graph upsert should have data_class="person"
        assert len(kg_calls) == 1
        assert kg_calls[0]["data_class"] == "person"
