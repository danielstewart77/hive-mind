"""Unit tests for metadata enforcement in memory_store and memory_store_direct."""

import json
import logging
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_neo4j_and_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure agents.memory can be imported by mocking neo4j and keyring."""
    # Mock neo4j if not available
    if "neo4j" not in sys.modules:
        neo4j_mock = MagicMock()
        monkeypatch.setitem(sys.modules, "neo4j", neo4j_mock)
    # Mock agent_tooling if not available


def _make_mock_driver() -> MagicMock:
    """Create a mock Neo4j driver with session and run mocked."""
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
    mock_result = MagicMock()
    mock_result.single.return_value = {"id": "test-id-123"}
    mock_session.run.return_value = mock_result
    return mock_driver


class TestMemoryStoreDirectMetadata:
    """Tests for memory_store_direct with metadata parameters."""

    def test_memory_store_direct_with_data_class_includes_metadata(self) -> None:
        mock_driver = _make_mock_driver()
        import tools.stateful.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_store_direct(
                content="Daniel prefers dark mode",
                tags="preference",
                source="user",
                data_class="preference",
            )
            result = json.loads(result_str)
            assert result["stored"] is True
            assert result["data_class"] == "preference"

            # Check that the Cypher query params include metadata
            mock_session = mock_driver.session.return_value.__enter__.return_value
            call_args = mock_session.run.call_args
            params = call_args[1]
            assert params["data_class"] == "preference"
            assert params["tier"] == "durable"
            assert params["superseded"] is False
            assert params["as_of"] is not None

    def test_memory_store_direct_unknown_class_returns_prompt(self) -> None:
        mock_driver = _make_mock_driver()
        import tools.stateful.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_store_direct(
                content="some content",
                data_class="unknown-class",
            )
            result = json.loads(result_str)
            assert result["stored"] is False
            assert "unknown-class" in result.get("prompt", "").lower() or "unknown-class" in result.get("error", "").lower()

    def test_memory_store_direct_without_data_class_raises_type_error(self) -> None:
        import tools.stateful.memory as mem_mod

        with pytest.raises(TypeError):
            mem_mod.memory_store_direct(
                content="something without class",
                source="user",
            )

    def test_memory_store_direct_timed_event_without_expires_returns_error(self) -> None:
        mock_driver = _make_mock_driver()
        import tools.stateful.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_store_direct(
                content="Meeting at 3pm",
                data_class="timed-event",
                source="user",
            )
            result = json.loads(result_str)
            assert result["stored"] is False

    def test_memory_store_direct_timed_event_with_expires_includes_field(self) -> None:
        mock_driver = _make_mock_driver()
        import tools.stateful.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_store_direct(
                content="Meeting at 3pm",
                data_class="timed-event",
                source="user",
                expires_at="2026-04-01T00:00:00Z",
            )
            result = json.loads(result_str)
            assert result["stored"] is True
            # Check cypher params include expires_at
            mock_session = mock_driver.session.return_value.__enter__.return_value
            call_args = mock_session.run.call_args
            params = call_args[1]
            assert params["expires_at"] == "2026-04-01T00:00:00Z"

    def test_memory_store_direct_invalid_source_returns_error(self) -> None:
        mock_driver = _make_mock_driver()
        import tools.stateful.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_store_direct(
                content="something",
                source="random",
                data_class="person",
            )
            result = json.loads(result_str)
            assert result["stored"] is False

    def test_memory_store_return_includes_data_class_in_response(self) -> None:
        mock_driver = _make_mock_driver()
        import tools.stateful.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_store_direct(
                content="Test content",
                source="user",
                data_class="person",
            )
            result = json.loads(result_str)
            assert "data_class" in result
            assert result["data_class"] == "person"


class TestMemoryStoreWithHITL:
    """Tests for memory_store (HITL-gated) with metadata pass-through."""

    def test_memory_store_with_hitl_passes_data_class_through(self) -> None:
        mock_driver = _make_mock_driver()
        import tools.stateful.memory as mem_mod

        with (
            patch.object(mem_mod, "_hitl_gate", return_value=True),
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_store(
                content="Daniel prefers dark mode",
                source="user",
                data_class="preference",
            )
            result = json.loads(result_str)
            assert result["stored"] is True
            assert result["data_class"] == "preference"
