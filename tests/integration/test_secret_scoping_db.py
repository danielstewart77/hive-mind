"""Integration tests for secret_scopes table in the broker database.

Verifies table creation and data persistence across DB connections.
"""

import asyncio

import pytest


def _run(coro):
    """Helper to run an async function synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)


class TestSecretScopesTable:
    """Integration tests for the secret_scopes table lifecycle."""

    def test_secret_scopes_table_created_on_init(self, tmp_path):
        """init_db() creates the secret_scopes table."""
        import core.broker as broker_mod

        db_path = str(tmp_path / "broker.db")
        db = _run(broker_mod.init_db(db_path))
        try:
            row = _run(db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='secret_scopes'"
            ))
            result = _run(row.fetchone())
            assert result is not None
            assert result["name"] == "secret_scopes"
        finally:
            _run(db.close())

    def test_secret_scopes_survive_db_reopen(self, tmp_path):
        """Granted scopes persist after closing and reopening the database."""
        import core.broker as broker_mod

        db_path = str(tmp_path / "broker.db")

        # First connection: grant a scope
        db1 = _run(broker_mod.init_db(db_path))
        _run(broker_mod.grant_secret_scope(db1, "ada", "MY_SECRET"))
        _run(db1.close())

        # Second connection: verify scope persists
        db2 = _run(broker_mod.init_db(db_path))
        try:
            result = _run(broker_mod.check_secret_scope(db2, "ada", "MY_SECRET"))
            assert result is True
        finally:
            _run(db2.close())
