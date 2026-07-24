"""Hive Nervous System — container entry point.

Runs two concurrent services:
1. lucent-api (FastAPI) — vector store + KG endpoints.
2. APScheduler in-process — fires scheduled tasks (prune-memory daily).

Both share the lucent SQLite store at $LUCENT_DB_PATH (mounted volume).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from lucent_api.server import app as lucent_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("nervous-system")


# ============================================================================
# Scheduled tasks
# ============================================================================

PRUNE_LOG_PATH = Path(
    os.environ.get("PRUNE_LOG_PATH", "/data/prune.log")
)


async def _run_prune() -> None:
    """Fire prune-memory and append the result to the daily log."""
    log.info("prune-memory: starting")
    try:
        from lucent_api.prune_memory import run_all
        runs = await asyncio.to_thread(run_all)
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
    )
    scheduler.start()
    log.info("scheduler started; prune-memory @ %s (%s)", prune_cron, prune_tz)

    # Lucent FastAPI
    lucent_cfg = uvicorn.Config(
        lucent_app, host="0.0.0.0", port=port, log_level="info"
    )
    lucent_server = uvicorn.Server(lucent_cfg)
    await lucent_server.serve()


if __name__ == "__main__":
    asyncio.run(main())
