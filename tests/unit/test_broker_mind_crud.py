"""Unit tests for broker mind CRUD operations: get_mind, update_mind, delete_mind."""

import asyncio
import time

import pytest


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def broker_db(tmp_path):
    """Create a temporary broker DB and return the connection."""
    from core.broker import init_db

    db_path = str(tmp_path / "broker.db")
    db = _run(init_db(db_path))
    yield db
    _run(db.close())


def _register(db, name="test", gateway_url="http://localhost:8420", model="sonnet", harness="claude_cli_claude"):
    """Helper to register a mind."""
    from core.broker import register_mind
    _run(register_mind(db, name=name, gateway_url=gateway_url, model=model, harness=harness))


class TestGetMind:
    """Tests for get_mind()."""

    def test_get_mind_returns_dict_for_existing(self, broker_db):
        from core.broker import get_mind

        _register(broker_db, name="test")
        result = _run(get_mind(broker_db, "test"))

        assert result is not None
        assert isinstance(result, dict)
        assert result["name"] == "test"
        assert result["gateway_url"] == "http://localhost:8420"
        assert result["model"] == "sonnet"
        assert result["harness"] == "claude_cli_claude"
        assert "registered_at" in result
        assert "last_seen" in result

    def test_get_mind_returns_none_for_missing(self, broker_db):
        from core.broker import get_mind

        result = _run(get_mind(broker_db, "nonexistent"))
        assert result is None


class TestUpdateMind:
    """Tests for update_mind()."""

    def test_update_mind_changes_specified_fields(self, broker_db):
        from core.broker import update_mind

        _register(broker_db, name="test", model="sonnet", gateway_url="http://localhost:8420")
        result = _run(update_mind(broker_db, "test", model="opus"))

        assert result is not None
        assert result["model"] == "opus"
        assert result["gateway_url"] == "http://localhost:8420"  # unchanged

    def test_update_mind_updates_last_seen(self, broker_db):
        from core.broker import get_mind, update_mind

        _register(broker_db, name="test")
        original = _run(get_mind(broker_db, "test"))
        original_last_seen = original["last_seen"]

        time.sleep(0.01)  # ensure time difference
        result = _run(update_mind(broker_db, "test", model="opus"))

        assert result["last_seen"] >= original_last_seen

    def test_update_mind_returns_none_for_missing(self, broker_db):
        from core.broker import update_mind

        result = _run(update_mind(broker_db, "nonexistent", model="opus"))
        assert result is None

    def test_update_mind_no_fields_still_updates_last_seen(self, broker_db):
        from core.broker import get_mind, update_mind

        _register(broker_db, name="test")
        original = _run(get_mind(broker_db, "test"))
        original_last_seen = original["last_seen"]

        time.sleep(0.01)
        result = _run(update_mind(broker_db, "test"))

        assert result is not None
        assert result["last_seen"] >= original_last_seen


class TestDeleteMind:
    """Tests for delete_mind()."""

    def test_delete_mind_removes_row(self, broker_db):
        from core.broker import delete_mind, get_mind

        _register(broker_db, name="test")
        deleted = _run(delete_mind(broker_db, "test"))

        assert deleted is True
        assert _run(get_mind(broker_db, "test")) is None

    def test_delete_mind_returns_false_for_missing(self, broker_db):
        from core.broker import delete_mind

        deleted = _run(delete_mind(broker_db, "nonexistent"))
        assert deleted is False
