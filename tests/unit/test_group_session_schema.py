"""Tests for group session schema in core/sessions.py."""


class TestGroupSessionSchema:
    """Verify _SCHEMA includes group session support."""

    def test_schema_includes_group_sessions_table(self):
        from core.sessions import _SCHEMA
        assert "CREATE TABLE IF NOT EXISTS group_sessions" in _SCHEMA

    def test_schema_group_sessions_has_required_columns(self):
        from core.sessions import _SCHEMA
        normalized = " ".join(_SCHEMA.split())
        assert "id TEXT PRIMARY KEY" in normalized
        assert "moderator_mind_id" in normalized
        assert "created_at" in normalized
        assert "ended_at" in normalized

    def test_sessions_table_has_group_session_id(self):
        from core.sessions import _SCHEMA
        assert "group_session_id" in _SCHEMA
