"""Unit tests for MCP server Lucent tool registration."""

import pytest


class TestMCPLucentRegistration:
    """Tests that MCP server imports Lucent tools."""

    def test_mcp_server_imports_lucent_kg_tools(self):
        """KG_TOOLS is imported from lucent_graph, not knowledge_graph."""
        from nervous_system.lucent_api.lucent_graph import KG_TOOLS

        assert len(KG_TOOLS) == 6
        names = {f.__name__ for f in KG_TOOLS}
        assert "graph_upsert" in names
        assert "graph_query" in names

    def test_mcp_server_imports_lucent_memory_tools(self):
        """MEMORY_TOOLS is imported from lucent_memory, not memory."""
        from nervous_system.lucent_api.lucent_memory import MEMORY_TOOLS

        assert len(MEMORY_TOOLS) == 6
        names = {f.__name__ for f in MEMORY_TOOLS}
        assert "memory_store" in names
        assert "memory_retrieve" in names

    def test_all_tool_names_registered(self):
        """All expected tool function names are present in the combined list."""
        from nervous_system.lucent_api.lucent_graph import KG_TOOLS
        from nervous_system.lucent_api.lucent_memory import MEMORY_TOOLS

        all_names = {f.__name__ for f in KG_TOOLS + MEMORY_TOOLS}
        expected = {
            "graph_upsert", "graph_upsert_direct", "graph_query",
            "search_person", "audit_person_nodes", "update_person_names",
            "memory_store", "memory_store_direct", "memory_list",
            "memory_delete", "memory_update", "memory_retrieve",
        }
        assert expected == all_names
