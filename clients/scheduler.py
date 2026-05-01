"""Hive Mind Scheduler — cron-driven proactive tasks.

Discovers scheduled skills from each mind's `.claude/skills/*/SKILL.md`
(via frontmatter `schedule:` field), runs them on their cron, and delivers
the result as a voice note (with text fallback) via Telegram.

Each fire creates a fresh session, sends the skill invocation, reads the
response, and kills the session. Sessions are not resumed across fires —
cross-day continuity comes from the mind's persistent memory layer
(knowledge graph, vector store), not from chat history.
"""

import asyncio
import json
import logging
import os
import re
import uuid
from pathlib import Path

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import config
from core.scheduled_skills import ScheduledSkill, discover_scheduled_skills

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
MINDS_ROOT = Path(os.environ.get("MINDS_ROOT", "/usr/src/app/minds"))

VOICE_SURFACE_PROMPT = (
    "You are responding via Telegram. Your responses will be spoken aloud as voice. "
    "CRITICAL: Do not use any special characters for formatting. No asterisks, no pound signs, "
    "no backticks, no hyphens as bullet points, no underscores for emphasis, no angle brackets, "
    "no pipes. Do not write code of any kind. Do not use numbered or bulleted lists. "
    "Write in plain flowing sentences, like natural speech."
)

DEV_SURFACE_PROMPT = (
    "You are running a scheduled autonomous task with full tool access. Work methodically. "
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


async def _try_send_voice(bot_token: str, chat_id: int, text: str, label: str) -> None:
    """Fire-and-forget: synthesise TTS and send voice note. Logs but never raises."""
    try:
        async with aiohttp.ClientSession() as http:
            audio = await _tts(http, text)
        await _send_voice(bot_token, chat_id, audio)
        log.info("Voice delivery complete for %s", label)
    except Exception:
        log.exception("Voice delivery failed for %s (text already sent)", label)


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


async def _create_session(http: aiohttp.ClientSession, skill: ScheduledSkill, surface_prompt: str) -> str:
    """Create a fresh session for this fire. Returns session id."""
    client_ref = f"scheduler-{skill.mind_id}-{skill.skill_name}-{uuid.uuid4().hex[:8]}"
    payload = {
        "owner_type": "scheduler",
        "owner_ref": str(config.telegram_owner_chat_id or "scheduler"),
        "client_ref": client_ref,
        "mind_id": skill.mind_id,
        "surface_prompt": surface_prompt,
    }
    async with http.post(f"{SERVER_URL}/sessions", json=payload) as resp:
        data = await resp.json()
    if "id" not in data:
        raise RuntimeError(f"Failed to create session: {data}")
    return data["id"]


async def _send_message(http: aiohttp.ClientSession, session_id: str, content: str) -> str:
    """Send a single message and consume the SSE stream into one combined string."""
    texts: list[str] = []
    result_fallback = ""
    sse_timeout = aiohttp.ClientTimeout(total=0, sock_read=0)
    async with http.post(
        f"{SERVER_URL}/sessions/{session_id}/message",
        json={"content": content},
        timeout=sse_timeout,
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Gateway message failed for {session_id}: HTTP {resp.status}")
        buf = ""
        async for chunk in resp.content.iter_any():
            buf += chunk.decode()
            while "\n" in buf:
                raw_line, buf = buf.split("\n", 1)
                raw_line = raw_line.strip()
                if not raw_line.startswith("data: "):
                    continue
                try:
                    event = json.loads(raw_line.removeprefix("data: "))
                except json.JSONDecodeError:
                    continue
                etype = event.get("type")
                if etype == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "text" and block.get("text"):
                            texts.append(block["text"])
                elif etype == "result":
                    result_fallback = event.get("result", "")
    return "\n\n".join(texts) or result_fallback or "(No response)"


async def _kill_session(http: aiohttp.ClientSession, session_id: str) -> None:
    """Best-effort delete of the session. Never raises."""
    try:
        async with http.delete(f"{SERVER_URL}/sessions/{session_id}") as resp:
            if resp.status >= 300:
                log.warning("Session %s delete returned HTTP %s", session_id, resp.status)
    except Exception:
        log.exception("Failed to delete session %s", session_id)


async def fire_skill(skill: ScheduledSkill) -> None:
    """Fire a single scheduled skill: fresh session → run → kill → deliver."""
    label = f"{skill.mind_id}/{skill.skill_name}"
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = config.telegram_owner_chat_id

    if skill.notify and (not bot_token or not chat_id):
        log.error("Cannot deliver %s — missing TELEGRAM_BOT_TOKEN or owner chat ID", label)
        return

    log.info("Firing %s", label)
    surface_prompt = VOICE_SURFACE_PROMPT if skill.voice else DEV_SURFACE_PROMPT

    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        session_id: str | None = None
        try:
            session_id = await _create_session(http, skill, surface_prompt)
            response = await _send_message(http, session_id, f"Run /{skill.skill_name}")
            response = _strip_markdown(response)
        except Exception:
            log.exception("Gateway failure for %s", label)
            if skill.notify:
                await _send_text(
                    bot_token, chat_id,
                    f"Scheduled task {label} failed to get a response.",
                )
            return
        finally:
            if session_id:
                await _kill_session(http, session_id)

    if not skill.notify:
        log.info("%s complete (notify=false, no delivery)", label)
        return

    await _send_text(bot_token, chat_id, response)
    if skill.voice:
        asyncio.create_task(_try_send_voice(bot_token, chat_id, response, label))


async def _memory_expiry_sweep() -> None:
    """Call the gateway's memory expiry sweep endpoint to process expired timed-events."""
    log.info("Running memory expiry sweep")
    try:
        timeout = aiohttp.ClientTimeout(total=120)
        headers = {"X-HITL-Internal": config.hitl_internal_token or ""}
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(f"{SERVER_URL}/memory/expiry-sweep", headers=headers) as resp:
                data = await resp.json()
                log.info(
                    "Memory expiry sweep: deleted=%d, prompted=%d, errors=%d",
                    data.get("deleted", 0),
                    data.get("prompted", 0),
                    data.get("errors", 0),
                )
    except Exception:
        log.exception("Memory expiry sweep failed")


async def _epilogue_sweep() -> None:
    """Call the gateway's epilogue sweep endpoint to process completed sessions."""
    log.info("Running epilogue sweep")
    try:
        timeout = aiohttp.ClientTimeout(total=120)
        headers = {"X-HITL-Internal": config.hitl_internal_token or ""}
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(f"{SERVER_URL}/epilogue/sweep", headers=headers) as resp:
                data = await resp.json()
                log.info(
                    "Epilogue sweep: processed=%d, auto_written=%d, skipped=%d, errors=%d, exceptions=%d",
                    data.get("processed", 0),
                    data.get("auto_written", 0),
                    data.get("skipped", 0),
                    data.get("errors", 0),
                    data.get("exceptions", 0),
                )
    except Exception:
        log.exception("Epilogue sweep failed")


RECONCILE_INTERVAL_SEC = 30
SKILL_JOB_PREFIX = "skill:"


def _skill_job_id(skill: ScheduledSkill) -> str:
    """Encode skill identity + schedule into the job id, so any change to the
    schedule produces a different job id and triggers a clean replace.
    """
    return f"{SKILL_JOB_PREFIX}{skill.mind_id}/{skill.skill_name}|{skill.cron}|{skill.timezone}|v={skill.voice}|n={skill.notify}"


def _reconcile_skill_jobs(scheduler: AsyncIOScheduler) -> tuple[int, int, int]:
    """Sync APScheduler's skill-job set to the current on-disk discovery.

    Returns (added, removed, total) for logging. Sweep jobs are never touched.
    """
    discovered = discover_scheduled_skills(MINDS_ROOT)
    desired_ids = {_skill_job_id(s): s for s in discovered}

    existing_ids = {
        job.id for job in scheduler.get_jobs()
        if job.id.startswith(SKILL_JOB_PREFIX)
    }

    to_remove = existing_ids - desired_ids.keys()
    to_add = [s for jid, s in desired_ids.items() if jid not in existing_ids]

    for jid in to_remove:
        scheduler.remove_job(jid)
        log.info("Unscheduled %s", jid.removeprefix(SKILL_JOB_PREFIX))

    for skill in to_add:
        parts = skill.cron.split()
        trigger = CronTrigger(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=parts[4],
            timezone=skill.timezone,
        )
        scheduler.add_job(
            fire_skill, trigger, args=[skill],
            id=_skill_job_id(skill),
        )
        log.info(
            "Scheduled %s/%s @ %s (%s)",
            skill.mind_id, skill.skill_name, skill.cron, skill.timezone,
        )

    return len(to_add), len(to_remove), len(desired_ids)


async def _reconcile_loop(scheduler: AsyncIOScheduler) -> None:
    """Re-sync skill jobs to disk every RECONCILE_INTERVAL_SEC seconds."""
    while True:
        await asyncio.sleep(RECONCILE_INTERVAL_SEC)
        try:
            added, removed, _total = _reconcile_skill_jobs(scheduler)
            if added or removed:
                log.info("Reconcile: +%d / -%d skill job(s)", added, removed)
        except Exception:
            log.exception("Skill reconcile failed")


async def main() -> None:
    scheduler = AsyncIOScheduler()

    added, _removed, total = _reconcile_skill_jobs(scheduler)
    if total == 0:
        log.warning(
            "No scheduled skills found under %s — scheduler will only run sweep jobs",
            MINDS_ROOT,
        )

    memory_expiry_trigger = CronTrigger(hour="3", minute="30", timezone="America/Chicago")
    scheduler.add_job(_memory_expiry_sweep, memory_expiry_trigger, id="memory-expiry-sweep")
    log.info("Scheduled memory expiry sweep @ 30 3 * * *")

    epilogue_trigger = CronTrigger(minute="*/15", timezone="America/Chicago")
    scheduler.add_job(_epilogue_sweep, epilogue_trigger, id="epilogue-sweep")
    log.info("Scheduled epilogue sweep @ */15 * * * *")

    scheduler.start()
    log.info(
        "Scheduler running — %d skill job(s) + memory expiry sweep + epilogue sweep "
        "(reconcile every %ds)",
        total, RECONCILE_INTERVAL_SEC,
    )

    asyncio.create_task(_reconcile_loop(scheduler))

    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
