"""Unit tests for memory_retrieve metadata surfacing."""

import json
from unittest.mock import MagicMock, patch


def _make_mock_driver() -> MagicMock:
    """Create a mock Neo4j driver."""
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return mock_driver


def _make_record(**fields: object) -> MagicMock:
    """Create a mock Neo4j record with dict-like access."""
    record = MagicMock()
    record.__getitem__ = lambda self, key: fields[key]
    return record


class TestMemoryRetrieveMetadata:
    """Tests for metadata fields in memory_retrieve results."""

    def test_memory_retrieve_includes_data_class_in_results(self) -> None:
        import agents.memory as mem_mod

        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value
        record = _make_record(
            content="Test memory",
            tags="test",
            source="user",
            agent_id="ada",
            created_at=1000,
            score=0.95,
            data_class="person",
            tier="durable",
            as_of="2026-01-01T00:00:00Z",
            expires_at=None,
            superseded=False,
        )
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([record]))
        mock_session.run.return_value = mock_result

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_retrieve(query="test")
            result = json.loads(result_str)
            assert result["count"] == 1
            assert result["memories"][0]["data_class"] == "person"

    def test_memory_retrieve_includes_tier_in_results(self) -> None:
        import agents.memory as mem_mod

        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value
        record = _make_record(
            content="Test",
            tags="",
            source="user",
            agent_id="ada",
            created_at=1000,
            score=0.9,
            data_class="preference",
            tier="durable",
            as_of="2026-01-01",
            expires_at=None,
            superseded=False,
        )
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([record]))
        mock_session.run.return_value = mock_result

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_retrieve(query="test")
            result = json.loads(result_str)
            assert result["memories"][0]["tier"] == "durable"

    def test_memory_retrieve_includes_as_of_in_results(self) -> None:
        import agents.memory as mem_mod

        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value
        record = _make_record(
            content="Test",
            tags="",
            source="user",
            agent_id="ada",
            created_at=1000,
            score=0.9,
            data_class="person",
            tier="durable",
            as_of="2026-03-01T12:00:00Z",
            expires_at=None,
            superseded=False,
        )
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([record]))
        mock_session.run.return_value = mock_result

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_retrieve(query="test")
            result = json.loads(result_str)
            assert result["memories"][0]["as_of"] == "2026-03-01T12:00:00Z"

    def test_memory_retrieve_handles_entries_without_metadata(self) -> None:
        """Pre-migration entries should return None for metadata fields."""
        import agents.memory as mem_mod

        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value

        # Simulate a pre-migration record that has None for metadata fields
        record = _make_record(
            content="Legacy entry",
            tags="old",
            source="user",
            agent_id="ada",
            created_at=500,
            score=0.8,
            data_class=None,
            tier=None,
            as_of=None,
            expires_at=None,
            superseded=None,
        )
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([record]))
        mock_session.run.return_value = mock_result

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_retrieve(query="legacy")
            result = json.loads(result_str)
            assert result["count"] == 1
            memory = result["memories"][0]
            assert memory["data_class"] is None
            assert memory["tier"] is None
            assert memory["as_of"] is None
