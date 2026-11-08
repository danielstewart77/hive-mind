"""Hive Nervous System — container entry point.

Runs two concurrent services:
1. lucent-api (FastAPI) — vector store + KG endpoints.
2. APScheduler in-process — fires scheduled tasks (prune-memory daily).

Both reach the hive's one database through `hive_data`. The store is opened
once here, before anything serves, and closed once at shutdown: `hive_data`
opens no pool on demand, and a lazy open under two concurrent first calls
builds two pools holding double the connections against a ceiling shared with
every other service.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from lucent_api.server import app as lucent_app
from hive_logging import configure_logging

log = configure_logging("nervous-system")


# ============================================================================
# Scheduled tasks
# ============================================================================

PRUNE_LOG_PATH = Path(
    os.environ.get("PRUNE_LOG_PATH", "/data/prune.log")
)

#: How long one prune pass may take before it is abandoned.
#:
#: The pass classifies through Ollama over HTTP, and an inference host that
#: accepts a connection and then never answers leaves this coroutine awaiting
#: forever. Without a deadline that is the end of pruning on this install —
#: APScheduler will not start a second instance of a job while the first is
#: still running, so every night after it is skipped, and the only trace is
#: an absence in a log nobody tails. An hour is far past any real pass and
#: far short of a week.
PRUNE_DEADLINE_SECONDS = float(
    os.environ.get("PRUNE_DEADLINE_SECONDS", str(60 * 60))
)


async def _run_prune() -> None:
    """Fire prune-memory and append the result to the daily log.

    Awaited rather than run through `asyncio.to_thread`. The thread was
    there because the prune opened its own `sqlite3` connection; now that it
    reads through the access layer, a thread is the one thing that cannot
    work — `hive_data` raises on a pool reached from another event loop, and
    a nested `asyncio.run` gets a pool bound to a loop that is already
    closed. Either way it fails once a night inside the handler below, into
    a log nobody tails.
    """
    log.info("prune-memory: starting")
    try:
        from lucent_api.prune_memory import run_all
        runs = await asyncio.wait_for(run_all(),
                                      timeout=PRUNE_DEADLINE_SECONDS)
    except asyncio.TimeoutError:
        # Said out loud, because the alternative is silence: a pass that
        # never returns is a job APScheduler then skips every night, and
        # nothing else in this process notices that memory stopped being
        # pruned.
        log.error("prune-memory: abandoned after %.0fs — the pass did not "
                  "finish. Rows read before the deadline are not deleted. "
                  "Check whether the inference host is answering.",
                  PRUNE_DEADLINE_SECONDS)
        return
    except Exception:
        log.exception("prune-memory: run_all() crashed")
        return

    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "runs": runs,
    }
    try:
        PRUNE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with PRUNE_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        log.exception("prune-memory: failed writing log")

    summary = ", ".join(
        f"{r['data_class']}: {len(r.get('deleted', []))} deleted, {r.get('kept', 0)} kept"
        for r in runs
    )
    log.info("prune-memory: done — %s", summary)
    if runs and not any(r.get("read", 0) for r in runs):
        # A pass that read nothing at all is the shape the cutover produces
        # when the prune is pointed at a store nobody writes: four pruners
        # each reporting zero deleted and zero kept, which is
        # indistinguishable in the log from a quiet month. Said out loud
        # once rather than inferred later.
        log.error(
            "prune-memory: every pruner read zero rows. That is what a "
            "prune pointed at the wrong store looks like, not a quiet "
            "night — check which database the access layer is opened "
            "against.")


# ============================================================================
# Main
# ============================================================================

async def main() -> None:
    port = int(os.environ.get("LUCENT_API_PORT", "8424"))

    # Scheduler
    scheduler = AsyncIOScheduler()
    prune_cron = os.environ.get("PRUNE_CRON", "0 4 * * *")
    prune_tz = os.environ.get("PRUNE_TIMEZONE", "America/Chicago")
    parts = prune_cron.split()
    if len(parts) != 5:
        log.error("invalid PRUNE_CRON: %r — using default '0 4 * * *'", prune_cron)
        parts = ["0", "4", "*", "*", "*"]
    scheduler.add_job(
        _run_prune,
        CronTrigger(
            minute=parts[0], hour=parts[1], day=parts[2],
            month=parts[3], day_of_week=parts[4],
            timezone=prune_tz,
        ),
        id="prune-memory",
        replace_existing=True,
        # One pass at a time, and a run that missed its slot is one run
        # rather than a backlog of them. Both are APScheduler's defaults and
        # both are stated: a prune of thousands of rows overlapping itself
        # would have two passes reading the same rows and each deleting what
        # the other already counted.
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info("scheduler started; prune-memory @ %s (%s)", prune_cron, prune_tz)

    # Lucent FastAPI
    lucent_cfg = uvicorn.Config(
        lucent_app, host="0.0.0.0", port=port, log_level="info",
        log_config=None, access_log=False,
    )
    lucent_server = uvicorn.Server(lucent_cfg)
    await lucent_server.serve()


if __name__ == "__main__":
    asyncio.run(main())
