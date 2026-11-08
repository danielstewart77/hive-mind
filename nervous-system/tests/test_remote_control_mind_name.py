"""The name a Remote Control conversation is started under.

`claude --remote-control --name <X>` is how the operator picks a conversation
out of a list, so X has to be the mind's own name. It was
`row["mind_id"].capitalize()`, which on a uuid-keyed row is a capitalised
uuid, and which fell back to `"ada"` on a row naming no mind — putting one
mind's name on another's conversation, or on one belonging to nobody.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from comms_pg_fixture import (  # noqa: F401 - fixtures
    close_manager,
    comms_db,
    execute,
    make_manager,
    register_mind,
    scratch_dsn,
)

pytestmark = pytest.mark.usefixtures("comms_db")


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


async def _manager():
    mgr = await make_manager()
    await register_mind(
        SKIPPY, name="skippy", gateway_url="http://127.0.0.1:8421",
        model="claude-opus-5", harness="claude",
    )
    return mgr


async def _insert(session_id, mind_id):
    now = time.time()
    await execute(
        "INSERT INTO core.sessions (id, conversation_id, surface, surface_id, model,"
        " created_at, last_active, status, mind_id)"
        " VALUES ($1, 'conv-1', 'web', 'u1', 'claude-opus-5', $2, $2, 'idle', $3)",
        session_id, now, mind_id,
    )


def test_remote_control_names_the_conversation_after_the_registered_mind():
    async def scenario():
        mgr = await _manager()
        await _insert("s1", SKIPPY)
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
            await close_manager(mgr)

        assert spawned, "no process was spawned"
        cmd = spawned[0]
        assert cmd[cmd.index("--name") + 1] == "skippy"

    _run(scenario())


def test_a_conversation_naming_no_mind_is_refused_rather_than_resumed():
    async def scenario():
        mgr = await _manager()
        await _insert("s2", None)
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
            await close_manager(mgr)

        assert spawned == []

    _run(scenario())
