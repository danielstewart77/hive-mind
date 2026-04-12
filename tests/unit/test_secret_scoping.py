"""Unit tests for secret scoping policy functions in core/broker.py.

Tests grant, revoke, check, and list operations for the secret_scopes table.
"""

import asyncio

import pytest


def _run(coro):
    """Helper to run an async function synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def scoped_db(tmp_path):
    """Provide an initialized broker DB with secret_scopes table."""
    import core.broker as broker_mod
    db_path = str(tmp_path / "broker.db")
    db = _run(broker_mod.init_db(db_path))
    yield db
    _run(db.close())


class TestGrantSecretScope:
    """Tests for grant_secret_scope()."""

    def test_grant_secret_scope_inserts_row(self, scoped_db):
        from core.broker import grant_secret_scope, check_secret_scope
        _run(grant_secret_scope(scoped_db, "ada", "DISCORD_BOT_TOKEN"))
        result = _run(check_secret_scope(scoped_db, "ada", "DISCORD_BOT_TOKEN"))
        assert result is True

    def test_grant_secret_scope_idempotent(self, scoped_db):
        from core.broker import grant_secret_scope
        _run(grant_secret_scope(scoped_db, "ada", "DISCORD_BOT_TOKEN"))
        # Should not raise on duplicate
        _run(grant_secret_scope(scoped_db, "ada", "DISCORD_BOT_TOKEN"))


class TestCheckSecretScope:
    """Tests for check_secret_scope()."""

    def test_check_secret_scope_returns_true_when_granted(self, scoped_db):
        from core.broker import grant_secret_scope, check_secret_scope
        _run(grant_secret_scope(scoped_db, "ada", "DISCORD_BOT_TOKEN"))
        assert _run(check_secret_scope(scoped_db, "ada", "DISCORD_BOT_TOKEN")) is True

    def test_check_secret_scope_returns_false_when_not_granted(self, scoped_db):
        from core.broker import check_secret_scope
        assert _run(check_secret_scope(scoped_db, "ada", "NONEXISTENT_KEY")) is False


class TestGetSecretScopes:
    """Tests for get_secret_scopes()."""

    def test_get_secret_scopes_returns_all_keys(self, scoped_db):
        from core.broker import grant_secret_scope, get_secret_scopes
        _run(grant_secret_scope(scoped_db, "ada", "KEY_A"))
        _run(grant_secret_scope(scoped_db, "ada", "KEY_B"))
        _run(grant_secret_scope(scoped_db, "ada", "KEY_C"))
        keys = _run(get_secret_scopes(scoped_db, "ada"))
        assert sorted(keys) == ["KEY_A", "KEY_B", "KEY_C"]


class TestRevokeSecretScope:
    """Tests for revoke_secret_scope()."""

    def test_revoke_secret_scope_removes_row(self, scoped_db):
        from core.broker import grant_secret_scope, revoke_secret_scope, check_secret_scope
        _run(grant_secret_scope(scoped_db, "ada", "KEY_A"))
        _run(revoke_secret_scope(scoped_db, "ada", "KEY_A"))
        assert _run(check_secret_scope(scoped_db, "ada", "KEY_A")) is False
