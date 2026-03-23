"""Unit tests for making data_class a required parameter."""

import sys
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _mock_neo4j_and_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    if "neo4j" not in sys.modules:
        neo4j_mock = MagicMock()
        monkeypatch.setitem(sys.modules, "neo4j", neo4j_mock)


class TestValidateDataClassRequired:
    """Tests for validate_data_class rejecting None."""

    def test_validate_data_class_none_raises_value_error(self) -> None:
        from core.memory_schema import validate_data_class
        with pytest.raises(ValueError, match="required"):
            validate_data_class(None)

    def test_build_metadata_requires_data_class(self) -> None:
        from core.memory_schema import build_metadata
        with pytest.raises(ValueError):
            build_metadata(data_class=None, source="user")


class TestMemoryStoreRequired:
    """Tests that memory_store and memory_store_direct require data_class."""

    def test_memory_store_without_data_class_raises(self) -> None:
        import tools.stateful.memory as mem_mod
        # memory_store requires data_class as a required argument now
        # Calling without it should raise TypeError
        with pytest.raises(TypeError):
            mem_mod.memory_store(content="test")

    def test_memory_store_direct_without_data_class_raises(self) -> None:
        import tools.stateful.memory as mem_mod
        with pytest.raises(TypeError):
            mem_mod.memory_store_direct(content="test")


class TestGraphUpsertRequired:
    """Tests that graph_upsert and graph_upsert_direct require data_class."""

    def test_graph_upsert_without_data_class_raises(self) -> None:
        import tools.stateful.knowledge_graph as kg_mod
        with pytest.raises(TypeError):
            kg_mod.graph_upsert(entity_type="Person", name="X")

    def test_graph_upsert_direct_without_data_class_raises(self) -> None:
        import tools.stateful.knowledge_graph as kg_mod
        with pytest.raises(TypeError):
            kg_mod.graph_upsert_direct(entity_type="Person", name="X")
