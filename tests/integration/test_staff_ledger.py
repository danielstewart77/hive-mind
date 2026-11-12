"""The chief of staff's ledger tool, against a real Postgres of its own.

Never the live `hive` database: these are the chief of staff's real rows.
The scratch database comes from `hive_data.testing`, claimed for the run and
migrated exactly as an install is, so the suite shares one sweep and one
allow-list with every other suite on the server.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import uuid
from pathlib import Path

import pytest

hive_data = pytest.importorskip(
    "hive_data", reason="hive_data is not installed in this environment")
asyncpg = pytest.importorskip("asyncpg")

from hive_data import migrate  # noqa: E402
from hive_data.testing import claimed_database  # noqa: E402

ADMIN_DSN = os.environ.get("HIVE_DATA_TEST_ADMIN_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not ADMIN_DSN,
    reason="HIVE_DATA_TEST_ADMIN_DSN is unset: it names a Postgres this "
           "suite may create and drop a database on. No default, because a "
           "default would be a credential in a tracked file.")

TOOL = (Path(__file__).resolve().parents[2]
        / "tools" / "stateless" / "staff_ledger" / "staff_ledger.py")


def _tool():
    spec = importlib.util.spec_from_file_location("staff_ledger", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_opening_a_task_creates_the_row_with_its_session_and_prints_its_id(
        monkeypatch, capsys):
    session_id = f"sess-{uuid.uuid4().hex}"
    async with claimed_database(ADMIN_DSN, "staff_ledger_test_") as dsn:
        await migrate.apply(dsn)
        monkeypatch.setenv("HIVE_DATABASE_URL", dsn)

        # On a thread: `main` runs its own event loop, as it does from a
        # shell, and this test's loop is already running.
        code = await asyncio.to_thread(_tool().main, [
            "open", "--name", "Answer the church question",
            "--session", session_id,
            "--description", "What time is the potluck on Sunday?"])

        printed = json.loads(capsys.readouterr().out)
        assert code == 0
        connection = await asyncpg.connect(dsn)
        try:
            task = await connection.fetchrow(
                "SELECT name, description, status FROM staff.tasks "
                "WHERE id = $1", printed["id"])
            pairs = await connection.fetch(
                "SELECT session_id FROM staff.task_sessions "
                "WHERE task_id = $1", printed["id"])
        finally:
            await connection.close()
        assert task is not None
        assert dict(task) == {
            "name": "Answer the church question",
            "description": "What time is the potluck on Sunday?",
            "status": "in progress"}
        assert [p["session_id"] for p in pairs] == [session_id]
