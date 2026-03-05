"""Unit tests for epilogue write_to_memory with data_class and valid source."""

import asyncio
import json
import sys
from unittest.mock import MagicMock, patch

import pytest

# Ensure agents can be imported
@pytest.fixture(autouse=True)
def _mock_neo4j_and_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    if "neo4j" not in sys.modules:
        neo4j_mock = MagicMock()
        monkeypatch.setitem(sys.modules, "neo4j", neo4j_mock)
    if "agent_tooling" not in sys.modules:
        at_mock = MagicMock()
        at_mock.tool = MagicMock(return_value=lambda f: f)
        monkeypatch.setitem(sys.modules, "agent_tooling", at_mock)


def _make_mock_driver() -> MagicMock:
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
    mock_result = MagicMock()
    mock_result.single.return_value = {"id": "test-id"}
    mock_session.run.return_value = mock_result
    return mock_driver


class TestEpilogueWriteSource:
    """Tests that epilogue uses valid source."""

    def test_epilogue_write_memory_uses_valid_source(self) -> None:
        import agents.memory as mem_mod

        calls: list[dict] = []

        def capture_store(**kwargs):
            calls.append(kwargs)
            return json.dumps({"stored": True, "id": "test", "data_class": kwargs.get("data_class")})

        digest = {
            "topics": ["Session covered dark mode preferences."],
            "entities": [],
            "relationships": [],
        }

        with (
            patch("agents.memory.memory_store_direct", side_effect=capture_store),
            patch("agents.knowledge_graph.graph_upsert_direct", return_value=json.dumps({"upserted": True})),
        ):
            from core.epilogue import write_to_memory
            asyncio.run(write_to_memory(digest))

        assert len(calls) == 1
        # Source should be "session" (valid), not "session_epilogue" (invalid)
        assert calls[0]["source"] == "session"

    def test_epilogue_write_memory_defaults_data_class_to_session_log(self) -> None:
        calls: list[dict] = []

        def capture_store(**kwargs):
            calls.append(kwargs)
            return json.dumps({"stored": True, "id": "test", "data_class": kwargs.get("data_class")})

        digest = {
            "topics": ["Topic one about testing."],
            "entities": [],
            "relationships": [],
        }

        with (
            patch("agents.memory.memory_store_direct", side_effect=capture_store),
            patch("agents.knowledge_graph.graph_upsert_direct", return_value=json.dumps({"upserted": True})),
        ):
            from core.epilogue import write_to_memory
            asyncio.run(write_to_memory(digest))

        assert calls[0]["data_class"] == "session-log"


class TestEpilogueWriteEntityClassification:
    """Tests that entities written by epilogue get inferred data_class."""

    def test_epilogue_write_entity_infers_data_class_from_type(self) -> None:
        calls: list[dict] = []

        def capture_upsert(**kwargs):
            calls.append(kwargs)
            return json.dumps({"upserted": True, "id": "test"})

        digest = {
            "topics": [],
            "entities": [
                {"name": "Daniel", "type": "person", "context": "Owner of Hive Mind"},
            ],
            "relationships": [],
        }

        with (
            patch("agents.memory.memory_store_direct", return_value=json.dumps({"stored": True})),
            patch("agents.knowledge_graph.graph_upsert_direct", side_effect=capture_upsert),
        ):
            from core.epilogue import write_to_memory
            asyncio.run(write_to_memory(digest))

        assert len(calls) == 1
        assert calls[0]["data_class"] == "person"

    def test_epilogue_write_entity_type_preference_gets_preference_class(self) -> None:
        calls: list[dict] = []

        def capture_upsert(**kwargs):
            calls.append(kwargs)
            return json.dumps({"upserted": True, "id": "test"})

        digest = {
            "topics": [],
            "entities": [
                {"name": "Dark Mode", "type": "preference", "context": "UI preference"},
            ],
            "relationships": [],
        }

        with (
            patch("agents.memory.memory_store_direct", return_value=json.dumps({"stored": True})),
            patch("agents.knowledge_graph.graph_upsert_direct", side_effect=capture_upsert),
        ):
            from core.epilogue import write_to_memory
            asyncio.run(write_to_memory(digest))

        assert calls[0]["data_class"] == "preference"

    def test_epilogue_write_relationship_has_data_class(self) -> None:
        calls: list[dict] = []

        def capture_upsert(**kwargs):
            calls.append(kwargs)
            return json.dumps({"upserted": True, "id": "test"})

        digest = {
            "topics": [],
            "entities": [
                {"name": "Daniel", "type": "person", "context": "Owner"},
                {"name": "Hive Mind", "type": "project", "context": "System"},
            ],
            "relationships": [
                {"from": "Daniel", "edge": "MANAGES", "to": "Hive Mind"},
            ],
        }

        with (
            patch("agents.memory.memory_store_direct", return_value=json.dumps({"stored": True})),
            patch("agents.knowledge_graph.graph_upsert_direct", side_effect=capture_upsert),
        ):
            from core.epilogue import write_to_memory
            asyncio.run(write_to_memory(digest))

        # Last call should be the relationship upsert and should have data_class
        rel_calls = [c for c in calls if c.get("relation")]
        assert len(rel_calls) >= 1
        assert rel_calls[0].get("data_class") is not None
