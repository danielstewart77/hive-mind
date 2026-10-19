"""The name a Remote Control conversation is started under.

`claude --remote-control --name <X>` is how the operator picks a conversation
out of a list, so X has to be the mind's own name. It was
`row["mind_id"].capitalize()`, which on a uuid-keyed row is a capitalised
uuid, and which fell back to `"ada"` on a row naming no mind — putting one
mind's name on another's conversation, or on one belonging to nobody.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

import aiosqlite
import pytest

from comms import broker
from comms.sessions import SessionManager


def _run(coro):
    return asyncio.run(coro)


SKIPPY = "14cb820b-4a42-4f04-a593-54f532fd1d2f"


class _Proc:
    """A spawn that never produces a URL, so the call fails after the name is
    built. What is under test is the argument list, not the parsing."""

    pid = 4242

    def __init__(self):
        self.stdout = self
        self.stderr = self

    async def readline(self):
        return b""

    def kill(self):
        pass

    async def wait(self):
        return 0


async def _manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager()
    await mgr.start()
    broker_path = os.path.join(tmp, "broker.db")
    conn = await aiosqlite.connect(broker_path)
    conn.row_factory = aiosqlite.Row
    await conn.executescript(broker.MINDS_DDL)
    await conn.execute(
        "INSERT INTO minds (mind_id, name, owner, gateway_url, model, harness,"
        " registered_at, last_seen) VALUES (?, 'skippy', 'daniel',"
        " 'http://127.0.0.1:8421', 'claude-opus-5', 'claude', 0, 0)",
        (SKIPPY,),
    )
    await conn.commit()
    mgr.broker_db = conn
    return mgr


async def _insert(mgr, session_id, mind_id):
    now = time.time()
    await mgr._db.execute(
        "INSERT INTO sessions (id, claude_sid, owner_type, owner_ref, model,"
        " created_at, last_active, status, mind_id)"
        " VALUES (?, 'conv-1', 'web', 'u1', 'claude-opus-5', ?, ?, 'idle', ?)",
        (session_id, now, now, mind_id),
    )
    await mgr._db.commit()


def test_remote_control_names_the_conversation_after_the_registered_mind():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _insert(mgr, "s1", SKIPPY)
            spawned = []

            async def fake_exec(*cmd, **kwargs):
                spawned.append(cmd)
                return _Proc()

            asyncio.create_subprocess_exec  # noqa: B018  (named for the reader)
            import comms.sessions as sessions_mod

            original = sessions_mod.asyncio.create_subprocess_exec
            sessions_mod.asyncio.create_subprocess_exec = fake_exec
            try:
                with pytest.raises(RuntimeError):
                    await mgr.spawn_rc_process("s1", timeout=0.05)
            finally:
                sessions_mod.asyncio.create_subprocess_exec = original
                await mgr.shutdown()

            assert spawned, "no process was spawned"
            cmd = spawned[0]
            assert cmd[cmd.index("--name") + 1] == "skippy"

    _run(scenario())


def test_a_conversation_naming_no_mind_is_refused_rather_than_resumed():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _insert(mgr, "s2", None)
            spawned = []

            async def fake_exec(*cmd, **kwargs):
                spawned.append(cmd)
                return _Proc()

            import comms.sessions as sessions_mod

            original = sessions_mod.asyncio.create_subprocess_exec
            sessions_mod.asyncio.create_subprocess_exec = fake_exec
            try:
                with pytest.raises(ValueError):
                    await mgr.spawn_rc_process("s2", timeout=0.05)
            finally:
                sessions_mod.asyncio.create_subprocess_exec = original
                await mgr.shutdown()

            assert spawned == []

    _run(scenario())
