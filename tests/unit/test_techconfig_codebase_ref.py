"""Unit tests for codebase_ref field support in agents/memory.py."""

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


def _make_mock_driver() -> MagicMock:
    """Create a mock Neo4j driver that returns a result with an id."""
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
    mock_result = MagicMock()
    mock_result.single.return_value = {"id": "test-id-123"}
    mock_session.run.return_value = mock_result
    return mock_driver


class TestCodebaseRefInMemoryStore:
    """Tests for codebase_ref parameter in memory_store and memory_store_direct."""

    def test_memory_store_direct_accepts_codebase_ref(self) -> None:
        """Passing codebase_ref='server.py' results in the Cypher query receiving the param."""
        mock_driver = _make_mock_driver()
        import agents.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_store_direct(
                content="server.py uses FastAPI gateway on port 8420",
                data_class="technical-config",
                source="self",
                codebase_ref="server.py",
            )
            result = json.loads(result_str)
            assert result["stored"] is True

            # Verify codebase_ref was passed to Neo4j
            mock_session = mock_driver.session.return_value.__enter__.return_value
            call_args = mock_session.run.call_args
            params = call_args[1]
            assert params["codebase_ref"] == "server.py"

    def test_memory_store_direct_codebase_ref_defaults_to_none(self) -> None:
        """Omitting codebase_ref passes None to Cypher."""
        mock_driver = _make_mock_driver()
        import agents.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_store_direct(
                content="config.yaml stores non-secret settings",
                data_class="technical-config",
                source="self",
            )
            result = json.loads(result_str)
            assert result["stored"] is True

            mock_session = mock_driver.session.return_value.__enter__.return_value
            call_args = mock_session.run.call_args
            params = call_args[1]
            assert params["codebase_ref"] is None

    def test_memory_store_codebase_ref_passed_through(self) -> None:
        """memory_store passes codebase_ref through to memory_store_direct."""
        mock_driver = _make_mock_driver()
        import agents.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
            patch.object(mem_mod, "_hitl_gate", return_value=True),
        ):
            result_str = mem_mod.memory_store(
                content="sessions.py manages process pool",
                data_class="technical-config",
                source="self",
                codebase_ref="core/sessions.py",
            )
            result = json.loads(result_str)
            assert result["stored"] is True

            mock_session = mock_driver.session.return_value.__enter__.return_value
            call_args = mock_session.run.call_args
            params = call_args[1]
            assert params["codebase_ref"] == "core/sessions.py"

    def test_memory_retrieve_returns_codebase_ref(self) -> None:
        """Retrieved memories include the codebase_ref field."""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)

        mock_record = {
            "content": "server.py uses FastAPI",
            "tags": "technical",
            "source": "self",
            "agent_id": "ada",
            "created_at": 1000000,
            "score": 0.95,
            "data_class": "technical-config",
            "tier": "reviewable",
            "as_of": "2026-03-01T00:00:00Z",
            "expires_at": None,
            "superseded": False,
            "codebase_ref": "server.py",
        }
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([mock_record]))
        mock_session.run.return_value = mock_result

        import agents.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_retrieve("FastAPI server")
            result = json.loads(result_str)
            assert result["count"] == 1
            assert result["memories"][0]["codebase_ref"] == "server.py"
