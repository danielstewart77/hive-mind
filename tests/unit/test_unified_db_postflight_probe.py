"""The marker a post-switch check writes, and the journal that undoes it.

Every check in `postflight` writes a value into Postgres, reads the running
service, and puts the original back. The window between the two is an HTTP
call whose timeout is per-socket-operation rather than a deadline, so a
service dribbling bytes holds it open — and the three columns marked are all
`NOT NULL` and load-bearing. A run killed in that window has to leave
something on disk saying what the value was, or the marker stays in a live
column with nobody able to say what it replaced.

The module carried two definitions of the probe for a while, and Python keeps
the last one: the copy that actually ran was the one with no journal, while
the module docstring, the README and `--restore` all described the other.
These tests are against the journalling one, which is now the only one.

Postgres stands in as a recording connection rather than a real server: what
is being guarded is the order — journal before write, restore before the
journal goes — and a real database would make the test about the server's
availability instead.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from unified_db import postflight


class _Recorder:
    """A connection that answers one value and records what it was asked."""

    def __init__(self, value: object):
        self.value = value
        self.statements: list[tuple[str, tuple]] = []
        self.journal_at_write: str | None = None

    async def fetchval(self, sql: str, *args: object) -> object:
        self.statements.append((sql, args))
        return self.value

    async def execute(self, sql: str, *args: object) -> None:
        self.statements.append((sql, args))
        if self.journal_at_write is None:
            path = postflight.journal_path()
            self.journal_at_write = (path.read_text() if path.exists()
                                     else "")


@pytest.fixture
def journal(tmp_path, monkeypatch):
    """The probe's journal, somewhere this test can read it."""
    path = tmp_path / "postflight-probe.json"
    monkeypatch.setenv(postflight.JOURNAL_VAR, str(path))
    return path


def test_what_the_probe_overwrote_is_on_disk_before_it_is_overwritten(
        journal):
    """Held only in memory, a kill inside the probe window leaves a marker in
    a live column with nothing saying what the value was."""
    conn = _Recorder("claude-opus-5")

    async def run() -> None:
        async with postflight._Probe(conn, "core.minds", "model", "mind_id",
                                     "m-1", "postflight-abc123"):
            pass

    asyncio.run(run())

    assert conn.journal_at_write, "the write happened before the journal did"
    record = json.loads(conn.journal_at_write)
    assert record["table"] == "core.minds"
    assert record["column"] == "model"
    assert record["original"] == "claude-opus-5"
    assert record["marker"] == "postflight-abc123"


def test_the_probe_puts_the_original_back_and_clears_its_journal(journal):
    """A journal left behind refuses the next run, so a probe that completed
    has to take its own away — and the value it put back is the one it read,
    not the marker."""
    conn = _Recorder("claude-opus-5")

    async def run() -> None:
        async with postflight._Probe(conn, "core.minds", "model", "mind_id",
                                     "m-1", "postflight-abc123"):
            pass

    asyncio.run(run())

    restored = [args for sql, args in conn.statements if "UPDATE" in sql][-1]
    assert restored[0] == "claude-opus-5"
    assert not journal.exists()


def test_a_probe_refuses_a_row_whose_value_is_null(journal):
    """Restoring a null through the same `UPDATE` would fail and leave the
    marker, so the probe declines rather than marking the row at all."""
    conn = _Recorder(None)

    async def run() -> None:
        async with postflight._Probe(conn, "core.minds", "model", "mind_id",
                                     "m-1", "postflight-abc123"):
            pass

    with pytest.raises(postflight.Absent):
        asyncio.run(run())

    assert not journal.exists()
    assert not [sql for sql, _ in conn.statements if "UPDATE" in sql]


def test_a_killed_run_is_restored_from_its_journal(journal):
    """What `--restore` is for: the marker is still in the column, and the
    only record of what it replaced is the file the probe wrote."""
    journal.write_text(json.dumps({
        "table": "core.minds", "column": "model", "key": "mind_id",
        "key_value": "m-1", "original": "claude-opus-5",
        "marker": "postflight-abc123",
    }))
    conn = _Recorder(None)

    said = asyncio.run(postflight.restore_journal(conn))

    put_back = [args for sql, args in conn.statements if "UPDATE" in sql]
    assert put_back == [("claude-opus-5", "m-1")]
    assert "claude-opus-5" in said
    assert not journal.exists()
