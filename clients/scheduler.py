"""
Hive Mind Scheduler — cron-driven proactive tasks.

Reads scheduled_tasks from config.yaml, runs each on its cron schedule,
queries the gateway for a response, and delivers it as a voice note
(with text fallback) via Telegram.
"""

import asyncio
import logging
import os
import re

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import config
from core.gateway_client import GatewayClient

# ---------------------------------------------------------------------------
# Keyring → env bridge: the scheduler needs TELEGRAM_BOT_TOKEN in os.environ
# for direct Telegram API calls (voice/text delivery).
# ---------------------------------------------------------------------------
_KEYRING_ENV_KEYS = ["TELEGRAM_BOT_TOKEN"]

try:
    import keyring as _kr
    for _k in _KEYRING_ENV_KEYS:
        if _k not in os.environ:
            _v = _kr.get_password("hive-mind", _k)
            if _v:
                os.environ[_k] = _v
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("hive-mind-scheduler")

SERVER_URL = os.environ.get("HIVE_MIND_SERVER_URL", f"http://localhost:{config.server_port}")
VOICE_SERVER_URL = os.environ.get("VOICE_SERVER_URL", "http://localhost:8422")

# Surface prompts by task type
VOICE_SURFACE_PROMPT = (
    "You are responding via Telegram. Your responses will be spoken aloud as voice. "
    "CRITICAL: Do not use any special characters for formatting. No asterisks, no pound signs, "
    "no backticks, no hyphens as bullet points, no underscores for emphasis, no angle brackets, "
    "no pipes. Do not write code of any kind. Do not use numbered or bulleted lists. "
    "Write in plain flowing sentences, like natural speech."
)

DEV_SURFACE_PROMPT = (
    "You are running a scheduled autonomous development task with full tool access "
    "(file read/write, bash, MCP tools). Work methodically and write real, working code. "
    "Your final text response will be delivered as a Telegram message — write it in plain "
    "prose summarizing what was accomplished. No markdown formatting in the response."
)


def _strip_markdown(text: str) -> str:
    text = re.sub(r"```[^\n]*\n(.*?)```", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def _tts(http: aiohttp.ClientSession, text: str) -> bytes:
    async with http.post(f"{VOICE_SERVER_URL}/tts", json={"text": text}) as resp:
        if resp.status != 200:
            raise RuntimeError(f"TTS error {resp.status}: {await resp.text()}")
        return await resp.read()


async def _send_voice(bot_token: str, chat_id: int, audio: bytes) -> None:
    form = aiohttp.FormData()
    form.add_field("voice", audio, filename="response.ogg", content_type="audio/ogg")
    async with aiohttp.ClientSession() as s:
        await s.post(
            f"https://api.telegram.org/bot{bot_token}/sendVoice",
            params={"chat_id": str(chat_id)},
            data=form,
        )


async def _send_text(bot_token: str, chat_id: int, text: str) -> None:
    limit = 4096
    for i in range(0, max(len(text), 1), limit):
        async with aiohttp.ClientSession() as s:
            await s.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": str(chat_id), "text": text[i : i + limit]},
            )


async def run_task(task_index: int) -> None:
    task = config.scheduled_tasks[task_index]
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = config.telegram_owner_chat_id

    if not bot_token or not chat_id:
        log.error("Cannot deliver task %d — missing TELEGRAM_BOT_TOKEN or owner chat ID", task_index)
        return

    log.info("Running task %d: %s", task_index, task.prompt[:80])

    surface_prompt = VOICE_SURFACE_PROMPT if task.voice else DEV_SURFACE_PROMPT

    # Long timeout — tasks may chain multiple tool calls (calendar + email, etc.)
    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        gateway = GatewayClient(
            http, SERVER_URL, "scheduler", surface_prompt=surface_prompt
        )
        try:
            response = _strip_markdown(
                await gateway.query(chat_id, f"scheduler-{task_index}", task.prompt)
            )
        except Exception:
            log.exception("Gateway query failed for task %d", task_index)
            await _send_text(bot_token, chat_id, "Scheduled task failed to get a response.")
            return

    if not task.notify:
        log.info("Task %d complete (notify=false, no delivery)", task_index)
        return

    if task.voice:
        try:
            async with aiohttp.ClientSession() as http:
                audio = await _tts(http, response)
            await _send_voice(bot_token, chat_id, audio)
            return
        except Exception:
            log.exception("Voice delivery failed for task %d, falling back to text", task_index)

    await _send_text(bot_token, chat_id, response)


async def main() -> None:
    if not config.scheduled_tasks:
        log.warning("No scheduled_tasks configured in config.yaml — nothing to do")
        return

    scheduler = AsyncIOScheduler()

    for i, task in enumerate(config.scheduled_tasks):
        parts = task.cron.split()
        if len(parts) != 5:
            log.error("Invalid cron expression for task %d: %r", i, task.cron)
            continue
        trigger = CronTrigger(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=parts[4],
            timezone=task.timezone,
        )
        scheduler.add_job(run_task, trigger, args=[i], id=f"task-{i}")
        log.info("Scheduled task %d @ %s (%s): %s", i, task.cron, task.timezone, task.prompt[:60])

    scheduler.start()
    log.info("Scheduler running — %d job(s) active", len(config.scheduled_tasks))

    await asyncio.Event().wait()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
