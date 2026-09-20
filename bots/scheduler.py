"""Hive Mind Scheduler — cron-driven proactive tasks.

Discovers scheduled skills from each mind's `.claude/skills/*/SKILL.md`
or `.codex/skills/*/SKILL.md` (via frontmatter `schedule:` field), runs
them on their cron, and delivers the result as a voice note (with text
fallback) via Telegram.

A task with no `discord_channel` creates a fresh session, sends the skill
invocation, reads the response, kills the session, and delivers over
Telegram. Continuity for those comes from the mind's persistent memory
layer, not from chat history.

A task that names a `discord_channel` works the other way round. The fire
is a nudge into the session already bound to that channel — the same
binding the Discord bot uses, `("discord", "<channel_id>")` in
`active_sessions` — so the conversation holds every previous fire and
everything Daniel said back. The session is never killed, and the response
is posted into the channel rather than sent to Telegram.
"""

import asyncio
import json
import os
import signal
import uuid
from pathlib import Path

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import config
from core import discord_delivery
from core.hive_logging import configure_logging, log_event
from core.scheduled_skills import (
    ScheduledSkill,
    discover_scheduled_skills,
    discover_scheduler_tasks,
)

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

log = configure_logging("hive-mind-scheduler")

SERVER_URL = os.environ.get("HIVE_MIND_SERVER_URL", f"http://localhost:{config.server_port}")
VOICE_SERVER_URL = os.environ.get("VOICE_SERVER_URL", "http://localhost:8422")
# The TTS server is GPU-bound and serialises requests. When several voice
# jobs fire on the same cron minute (e.g. the 6:30am batch), a long
# synthesis can queue ahead of a short one. aiohttp's silent 300s default
# total timeout was tripping the queued job; give it an explicit, generous
# ceiling instead.
VOICE_TTS_TIMEOUT_SECONDS = float(os.environ.get("VOICE_TTS_TIMEOUT_SECONDS", "600"))
MINDS_ROOT = Path(os.environ.get("MINDS_ROOT", "/usr/src/app/minds"))
SCHEDULER_TASKS_YAML = Path(os.environ.get(
    "SCHEDULER_TASKS_YAML",
    "/usr/src/app/bots/scheduled_tasks/tasks.yaml",
))
COMMS_BEARER_TOKEN = os.environ.get("COMMS_BEARER_TOKEN", "")
GATEWAY_AUTH_HEADERS = (
    {"Authorization": f"Bearer {COMMS_BEARER_TOKEN}"} if COMMS_BEARER_TOKEN else {}
)

# Longest silence a channel fire tolerates between stream events before it
# gives up. Generous — a briefing calls two calendars and a reminder skill —
# but finite, because the alternative is a job slot held for ever.
CHANNEL_FIRE_READ_TIMEOUT_SECONDS = float(
    os.environ.get("CHANNEL_FIRE_READ_TIMEOUT_SECONDS", "600")
)

# A gate is a cheap check that runs on the cron with no session and no mind
# involved, so a task that has nothing to say costs a subprocess rather than a
# conversation turn full of tool calls nobody will ever read. Exit zero means
# stay quiet. The signal code means fire. Everything else is a broken gate,
# and a broken gate is reported rather than swallowed — a silent alerter and a
# working one look identical from outside, and the difference is only visible
# on the day an alert does not arrive.
GATE_FIRE_EXIT_CODE = 10
GATE_TIMEOUT_SECONDS = float(os.environ.get("GATE_TIMEOUT_SECONDS", "120"))

GATE_FIRE = "fire"
GATE_QUIET = "quiet"
GATE_ERROR = "error"


async def run_gate(
    argv: tuple[str, ...], *, timeout: float = GATE_TIMEOUT_SECONDS,
) -> tuple[str, str]:
    """Run a gate command and classify what it said.

    Returns the outcome and a human reason for the error cases. Output is
    read to keep the pipe from filling and is never returned: the gate
    decides whether the mind is woken, not what it says once it is. Reading
    is bounded by the same timeout as the run.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return GATE_ERROR, f"could not run gate: {exc}"

    try:
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        # The process outlives the await unless it is killed here, and a gate
        # that hangs every hour would otherwise accumulate one orphan per fire.
        try:
            # The whole process group: a gate written as a shell wrapper
            # leaves its children running otherwise, one set per fire.
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()
        return GATE_ERROR, f"gate timed out after {timeout:g}s"

    code = proc.returncode
    tail = (stdout_bytes or b"").decode(errors="replace").strip()[-500:]
    if code == 0:
        return GATE_QUIET, ""
    if code == GATE_FIRE_EXIT_CODE:
        return GATE_FIRE, ""
    detail = f"gate exited {code}"
    if tail:
        detail = f"{detail}: {tail}"
    return GATE_ERROR, detail


async def _report_gate_failure(label: str, reason: str) -> None:
    """Tell Daniel a gate is broken. Telegram, not the task's own channel —
    the channel is for what the task has to say, and a gate that never runs
    has nothing to say there.
    """
    log_event(log, "scheduled_skill.gate_failed", task=label, reason=reason)
    log.error("Gate failed for %s — %s", label, reason)
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = config.telegram_owner_chat_id
    if not bot_token or not chat_id:
        return
    try:
        await _send_text(
            bot_token, chat_id,
            f"Scheduled task {label} gate failed: {reason}",
        )
    except Exception:
        log.exception("Could not report gate failure for %s", label)


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


async def _tts(http: aiohttp.ClientSession, text: str, voice_id: str) -> bytes:
    timeout = aiohttp.ClientTimeout(total=VOICE_TTS_TIMEOUT_SECONDS)
    async with http.post(
        f"{VOICE_SERVER_URL}/tts",
        json={"text": text, "voice_id": voice_id},
        timeout=timeout,
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"TTS error {resp.status}: {await resp.text()}")
        return await resp.read()


async def _try_send_voice(bot_token: str, chat_id: int, text: str, voice_id: str, label: str) -> None:
    """Fire-and-forget: synthesise TTS and send voice note. Logs but never raises."""
    try:
        async with aiohttp.ClientSession() as http:
            audio = await _tts(http, text, voice_id)
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
    client_ref = f"scheduler-{skill.mind_name}-{skill.skill_name}-{uuid.uuid4().hex[:8]}"
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


async def _ensure_channel_session(
    http: aiohttp.ClientSession, skill: ScheduledSkill, channel_id: str
) -> str:
    """Resolve the session bound to a Discord channel, creating one if absent.

    Deliberately the same two steps `GatewayClient.ensure_session` takes for
    an inbound Discord message — look for the active binding on
    ("discord", channel_id), create and bind only when there is none. Any
    other addressing here would mint a second session for a channel that
    already had one, and the reply Daniel types would land in whichever of
    them the bot happened to resolve.
    """
    async with http.get(
        f"{SERVER_URL}/sessions",
        params={"client_type": "discord", "client_ref": channel_id},
    ) as resp:
        if resp.status != 200:
            # Not "this channel has no session" — "I could not find out". A
            # bad bearer, or comms restarting on the stroke of the cron,
            # would otherwise fall through to creating one, and
            # `INSERT OR REPLACE INTO active_sessions` hands the channel's
            # binding to an empty conversation. The accumulated thread is
            # orphaned, Daniel's replies follow the new row, and the fire
            # reports success.
            raise RuntimeError(
                f"Could not resolve the session for channel {channel_id}: "
                f"HTTP {resp.status}"
            )
        for session in await resp.json():
            if session.get("is_active"):
                return session["id"]

    owner_ref = str(config.discord_allowed_users[0]) if config.discord_allowed_users else "scheduler"
    payload = {
        "owner_type": "discord",
        "owner_ref": owner_ref,
        "client_ref": channel_id,
        "mind_id": skill.mind_id,
        "surface_prompt": VOICE_SURFACE_PROMPT if skill.voice else DEV_SURFACE_PROMPT,
    }
    async with http.post(f"{SERVER_URL}/sessions", json=payload) as resp:
        data = await resp.json()
    if "id" not in data:
        raise RuntimeError(f"Failed to create channel session: {data}")
    return data["id"]


def _nudge(skill: ScheduledSkill) -> str:
    """The whole of what a channel fire says.

    It names the skill and nothing else. The skill carries its own
    instructions, so repeating them here would put a copy of them in the
    conversation on every fire — which is the cost this design exists to
    avoid, and it compounds daily in a thread that is never reset.
    """
    return f"Run your {skill.skill_name} skill now."


async def _fire_into_channel(skill: ScheduledSkill, channel_id: str) -> None:
    """Nudge the channel's session to run the skill and post what comes back.

    The nudge itself never reaches Discord. It is a turn in the
    conversation, not a message to Daniel — what he sees in the channel is
    the briefing.
    """
    label = f"{skill.mind_name}/{skill.skill_name}"
    token = discord_delivery.bot_token()
    if not token:
        log.error("Cannot deliver %s — no Discord bot token", label)
        return

    timeout = aiohttp.ClientTimeout(total=840)
    async with aiohttp.ClientSession(timeout=timeout, headers=GATEWAY_AUTH_HEADERS) as http:
        try:
            session_id = await _ensure_channel_session(http, skill, channel_id)
            response = await _send_message(
                http, session_id, _nudge(skill),
                read_timeout=CHANNEL_FIRE_READ_TIMEOUT_SECONDS,
            )
        except Exception:
            log.exception("Gateway failure for %s", label)
            return

    audio: bytes | None = None
    if skill.voice:
        try:
            async with aiohttp.ClientSession() as voice_http:
                audio = await _tts(voice_http, response, skill.mind_id)
        except Exception:
            # Text is the delivery; the voice note is an addition to it.
            log.exception("TTS failed for %s — posting text only", label)

    try:
        async with aiohttp.ClientSession() as http:
            await discord_delivery.post_to_channel(http, token, channel_id, response)
    except Exception:
        log.exception("Discord delivery failed for %s", label)
        log_event(log, "scheduled_skill.delivery_failed", mind_id=skill.mind_id,
                  mind_name=skill.mind_name, skill_name=skill.skill_name,
                  channel_id=channel_id)
        return

    delivered_voice = False
    if audio:
        # A separate request, after the text has landed. Attach Files is its
        # own per-channel permission and an ogg can be refused on size, so
        # riding the audio on the briefing's last chunk means one 403 costs
        # the tail of the briefing — or, on a single-chunk one, all of it.
        try:
            async with aiohttp.ClientSession() as http:
                await discord_delivery.post_audio(http, token, channel_id, audio)
            delivered_voice = True
        except Exception:
            log.exception("Voice delivery failed for %s (text already posted)", label)

    log_event(log, "scheduled_skill.completed", mind_id=skill.mind_id,
              mind_name=skill.mind_name, skill_name=skill.skill_name,
              response_chars=len(response or ""), notified=True,
              channel_id=channel_id, voice=delivered_voice)


async def _send_message(
    http: aiohttp.ClientSession, session_id: str, content: str,
    read_timeout: float | None = None,
) -> str:
    """Send a single message and consume the SSE stream into one combined string.

    `read_timeout` bounds the gap between events, not the turn. A channel
    fire passes one because APScheduler runs a job at `max_instances=1`: a
    turn that never ends holds the slot forever, so every later fire of that
    skill is skipped — while comms holds the session lock and the bot holds
    the channel lock, so Daniel typing gets "still processing" indefinitely.
    The channel dies whole, and the only evidence is a log line.
    """
    texts: list[str] = []
    result_fallback = ""
    events_seen = 0
    event_type_counts: dict[str, int] = {}
    last_event_type: str | None = None
    json_decode_errors = 0
    sse_timeout = aiohttp.ClientTimeout(total=0, sock_read=read_timeout or 0)
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
                    json_decode_errors += 1
                    continue
                events_seen += 1
                etype = event.get("type") or "<no-type>"
                event_type_counts[etype] = event_type_counts.get(etype, 0) + 1
                last_event_type = etype
                if etype == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "text" and block.get("text"):
                            texts.append(block["text"])
                elif etype == "result":
                    result_fallback = event.get("result", "")
    combined = "\n\n".join(texts) or result_fallback
    if not combined.strip():
        raise RuntimeError(
            f"Empty response from session {session_id}: "
            f"events_seen={events_seen}, types={event_type_counts}, "
            f"last_event_type={last_event_type}, json_decode_errors={json_decode_errors}, "
            f"result_fallback={result_fallback!r}"
        )
    return combined


async def _kill_session(http: aiohttp.ClientSession, session_id: str) -> None:
    """Best-effort delete of the session. Never raises."""
    try:
        async with http.delete(f"{SERVER_URL}/sessions/{session_id}") as resp:
            if resp.status >= 300:
                log.warning("Session %s delete returned HTTP %s", session_id, resp.status)
    except Exception:
        log.exception("Failed to delete session %s", session_id)


async def _fire_command(skill: ScheduledSkill) -> None:
    """Run a command-type scheduled task as a subprocess.

    Output goes to scheduler logs. Notify/voice are intentionally ignored
    here — these tasks talk to the system (broker, event_triage, etc.) on
    their own; they don't return text to deliver.
    """
    label = f"{skill.mind_name}/{skill.skill_name}"
    cmd = list(skill.command or ())
    if not cmd:
        log.error("Command task %s has empty command list", label)
        return
    log.info("Firing %s as command (argv_count=%d)", label, len(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout_bytes, _ = await proc.communicate()
        stdout_text = (stdout_bytes or b"").decode(errors="replace").strip()
        if proc.returncode == 0:
            log.info("%s exit=0 output_chars=%d", label, len(stdout_text))
        else:
            log.error("%s exit=%s output_chars=%d", label, proc.returncode, len(stdout_text))
    except Exception:
        log.exception("Command task %s failed", label)


async def fire_skill(skill: ScheduledSkill) -> None:
    """Fire a single scheduled skill: fresh session → run → kill → deliver."""
    if skill.gate_error:
        # Declared and unreadable. Every other failure here declines to fire;
        # treating a typo as "no gate" would make it the one task that fires
        # every hour instead of never.
        await _report_gate_failure(
            f"{skill.mind_name}/{skill.skill_name}", skill.gate_error,
        )
        return
    if skill.gate:
        label = f"{skill.mind_name}/{skill.skill_name}"
        outcome, reason = await run_gate(skill.gate)
        if outcome == GATE_QUIET:
            log_event(log, "scheduled_skill.gate_quiet", mind_id=skill.mind_id,
                      mind_name=skill.mind_name, skill_name=skill.skill_name)
            return
        if outcome == GATE_ERROR:
            await _report_gate_failure(label, reason)
            return
    if skill.command:
        await _fire_command(skill)
        return
    if skill.discord_channel:
        log_event(log, "scheduled_skill.started", mind_id=skill.mind_id,
                  mind_name=skill.mind_name, skill_name=skill.skill_name,
                  notify=True, voice=skill.voice,
                  channel_id=skill.discord_channel)
        await _fire_into_channel(skill, skill.discord_channel)
        return
    label = f"{skill.mind_name}/{skill.skill_name}"
    log_event(log, "scheduled_skill.started", mind_id=skill.mind_id,
              mind_name=skill.mind_name, skill_name=skill.skill_name,
              notify=skill.notify, voice=skill.voice)
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = config.telegram_owner_chat_id

    if skill.notify and (not bot_token or not chat_id):
        log.error("Cannot deliver %s — missing TELEGRAM_BOT_TOKEN or owner chat ID", label)
        return

    log.info("Firing %s", label)
    surface_prompt = VOICE_SURFACE_PROMPT if skill.voice else DEV_SURFACE_PROMPT

    timeout = aiohttp.ClientTimeout(total=840)
    async with aiohttp.ClientSession(timeout=timeout, headers=GATEWAY_AUTH_HEADERS) as http:
        session_id: str | None = None
        try:
            session_id = await _create_session(http, skill, surface_prompt)
            instructions_text: str | None = None
            if skill.instructions_path:
                try:
                    instructions_text = Path(skill.instructions_path).read_text()
                except OSError as exc:
                    log.warning(
                        "Could not read instructions for %s at %s: %s — "
                        "falling back to path-reference dispatch",
                        label, skill.instructions_path, exc,
                    )
            if instructions_text:
                dispatch_msg = (
                    f"You are running the scheduled task '{skill.skill_name}'. "
                    "Execute the following instructions exactly. Do not search "
                    "for a skill file — the instructions are embedded below.\n\n"
                    f"{instructions_text}"
                )
            else:
                dispatch_msg = (
                    f"Run the {skill.skill_name} skill. "
                    f"Read its instructions from {skill.skill_path} and follow them exactly."
                )
            response = await _send_message(http, session_id, dispatch_msg)
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
        log.info("%s response_chars=%d", label, len(response or ""))
        log_event(log, "scheduled_skill.completed", mind_id=skill.mind_id,
                  mind_name=skill.mind_name, skill_name=skill.skill_name,
                  response_chars=len(response or ""), notified=False)
        return

    await _send_text(bot_token, chat_id, response)
    if skill.voice:
        asyncio.create_task(_try_send_voice(bot_token, chat_id, response, skill.mind_id, label))


RECONCILE_INTERVAL_SEC = 30
SKILL_JOB_PREFIX = "skill:"


def _skill_job_id(skill: ScheduledSkill) -> str:
    """Encode skill identity + schedule into the job id, so any change to the
    schedule produces a different job id and triggers a clean replace.
    """
    return (
        f"{SKILL_JOB_PREFIX}{skill.mind_name}/{skill.skill_name}|{skill.cron}"
        f"|{skill.timezone}|v={skill.voice}|n={skill.notify}|c={skill.discord_channel or ''}"
        # Unit-separated, not space-joined: argv is a list precisely because
        # a space is data, and two different gates sharing one job id means a
        # gate edit reconciles to no change and the old command keeps running.
        f"|g={chr(31).join(skill.gate) if skill.gate else ''}|ge={skill.gate_error or ''}"
    )


def _reconcile_skill_jobs(scheduler: AsyncIOScheduler) -> tuple[int, int, int]:
    """Sync APScheduler's skill-job set to the current on-disk discovery.

    Merges two sources: SKILL.md frontmatter discovery (legacy, dispatched
    by path) and scheduler-owned YAML tasks (instructions embedded inline).
    YAML wins on collisions so a migrated task can co-exist with a stale
    SKILL.md during transition. Returns (added, removed, total) for
    logging. Sweep jobs are never touched.
    """
    skill_md_tasks = discover_scheduled_skills(MINDS_ROOT)
    yaml_tasks = discover_scheduler_tasks(SCHEDULER_TASKS_YAML, MINDS_ROOT)

    by_identity: dict[tuple[str, str], ScheduledSkill] = {}
    for task in skill_md_tasks:
        by_identity[(task.mind_name, task.skill_name)] = task
    for task in yaml_tasks:
        by_identity[(task.mind_name, task.skill_name)] = task

    discovered = list(by_identity.values())
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
            skill.mind_name, skill.skill_name, skill.cron, skill.timezone,
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

    scheduler.start()
    log.info(
        "Scheduler running — %d skill job(s) (reconcile every %ds)",
        total, RECONCILE_INTERVAL_SEC,
    )

    asyncio.create_task(_reconcile_loop(scheduler))

    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
