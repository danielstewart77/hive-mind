"""A prune that never finishes is abandoned, loudly, rather than forever.

The pass classifies through Ollama over HTTP. An inference host that accepts
the connection and then never answers leaves the pass awaiting with no
deadline of its own — and APScheduler will not start a second instance of a
job whose first is still running, so every night after it is skipped too.
The result is that pruning stops on this install and the only trace is an
absence in a log nobody tails.

What is stubbed is the pass, because the pass is the thing being bounded;
what is called is `_run_prune`, which is where the deadline lives.
"""

from __future__ import annotations

import asyncio
import logging

import pytest


@pytest.mark.asyncio
async def test_a_pass_that_does_not_finish_is_abandoned_at_the_deadline(
        monkeypatch, caplog, tmp_path):
    import lucent_api.prune_memory as prune_memory
    import server

    started = asyncio.Event()

    async def never_answers():
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(prune_memory, "run_all", never_answers)
    monkeypatch.setattr(server, "PRUNE_DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(server, "PRUNE_LOG_PATH", tmp_path / "prune.log")

    with caplog.at_level(logging.ERROR):
        # Bounded by this test too: without the deadline in `_run_prune`
        # this waits the full hour and the test times out rather than
        # failing, which is the same defect seen from the outside.
        await asyncio.wait_for(server._run_prune(), timeout=10)

    assert started.is_set()
    assert any("abandoned" in r.getMessage() for r in caplog.records)
    # Nothing is written to the log file for a pass that produced no runs,
    # so a reader cannot mistake an abandoned night for a quiet one.
    assert not (tmp_path / "prune.log").exists()
