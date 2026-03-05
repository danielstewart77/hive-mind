"""Unit tests for memory_retrieve archived entry filtering (agents.memory)."""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock neo4j and agent_tooling for importing agents.memory."""
    if "neo4j" not in sys.modules:
        neo4j_mock = MagicMock()
        monkeypatch.setitem(sys.modules, "neo4j", neo4j_mock)
    if "agent_tooling" not in sys.modules:
        at_mock = MagicMock()
        at_mock.tool = MagicMock(return_value=lambda f: f)
        monkeypatch.setitem(sys.modules, "agent_tooling", at_mock)


def _make_mock_driver_for_retrieve(records: list[dict]) -> MagicMock:
    """Create a mock Neo4j driver that returns given records."""
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)

    mock_result = MagicMock()
    mock_result.__iter__ = MagicMock(return_value=iter(records))
    mock_session.run.return_value = mock_result

    return mock_driver


class TestMemoryRetrieveArchived:
    """Tests for the include_archived parameter on memory_retrieve."""

    def test_memory_retrieve_excludes_archived_by_default(self) -> None:
        """The Cypher query includes an archived filter when include_archived=False."""
        mock_driver = _make_mock_driver_for_retrieve([])

        import agents.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            mem_mod.memory_retrieve("test query")

        mock_session = mock_driver.session.return_value.__enter__.return_value
        cypher = mock_session.run.call_args[0][0]
        assert "archived" in cypher.lower()
        # Should filter out archived entries by default
        assert "m.archived IS NULL OR m.archived = false" in cypher

    def test_memory_retrieve_includes_archived_when_flag_set(self) -> None:
        """The Cypher query does NOT filter on archived when include_archived=True."""
        mock_driver = _make_mock_driver_for_retrieve([])

        import agents.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            mem_mod.memory_retrieve("test query", include_archived=True)

        mock_session = mock_driver.session.return_value.__enter__.return_value
        cypher = mock_session.run.call_args[0][0]
        # Should NOT have the archived filter
        assert "m.archived IS NULL OR m.archived = false" not in cypher

    def test_memory_retrieve_signature_has_include_archived_param(self) -> None:
        """The function signature includes include_archived: bool = False."""
        import agents.memory as mem_mod
        import inspect
        sig = inspect.signature(mem_mod.memory_retrieve)
        assert "include_archived" in sig.parameters
        param = sig.parameters["include_archived"]
        assert param.default is False

    def test_memory_retrieve_excludes_archived_with_tag_filter(self) -> None:
        """The archived filter is also applied when tag_filter is provided."""
        mock_driver = _make_mock_driver_for_retrieve([])

        import agents.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            mem_mod.memory_retrieve("test query", tag_filter="news")

        mock_session = mock_driver.session.return_value.__enter__.return_value
        cypher = mock_session.run.call_args[0][0]
        assert "m.archived IS NULL OR m.archived = false" in cypher

    def test_memory_retrieve_returns_archived_field_in_results(self) -> None:
        """Each result includes the archived field value."""
        records = [
            {
                "content": "Test memory",
                "tags": "test",
                "source": "user",
                "agent_id": "ada",
                "created_at": 1700000000,
                "score": 0.95,
                "data_class": "world-event",
                "tier": "T3",
                "as_of": None,
                "expires_at": None,
                "superseded": False,
                "codebase_ref": None,
                "archived": False,
            },
        ]
        mock_driver = _make_mock_driver_for_retrieve(records)

        import agents.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result = json.loads(mem_mod.memory_retrieve("test query", include_archived=True))

        assert "memories" in result
        assert len(result["memories"]) == 1
        assert "archived" in result["memories"][0]
