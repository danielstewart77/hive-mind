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
binding the Discord bot uses, `("discord:<mind_id>", "<channel_id>")` in
`active_sessions` — so the conversation holds every previous fire and
everything Daniel said back. The session is never killed, and the response
is posted into the channel rather than sent to Telegram.
"""

import asyncio
import json
import logging
import os
import signal
import time
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


# ---------------------------------------------------------------------------
# Where a mind's own unsolicited turn goes
# ---------------------------------------------------------------------------
#: The chat each mind's bot answers, as the gateway records it, with the
#: moment it was read. Expiring, because this process is long-lived: an
#: operator who releases a bot and hands it to somebody else would otherwise
#: keep sending that mind's reminders to the previous person until the
#: scheduler was restarted. A read that failed is not cached at all — the
#: next fire asks again rather than running on an old failure.
_claimed_chats: dict[tuple[str, str], tuple[float, str]] = {}

#: How long one claim is reused. Shorter than the gap between fires for
#: everything but the busiest task, so in practice every fire asks.
CLAIM_TTL_SECONDS = 60.0


async def claimed_chat_id(mind_id: str, surface: str = "telegram") -> str:
    """The person this mind's own bot answers, or empty if nobody has claimed it.

    A reminder belongs to whoever asked for it, which is the owner of the
    mind that raised it — not one stack-wide operator chat. With one bot per
    person, that chat is exactly the claim on that mind's bot.

    Empty rather than a guess. Delivering a second person's reminder to the
    operator is both a disclosure and a reminder the person never gets.
    """
    key = (mind_id or "", surface)
    cached = _claimed_chats.get(key)
    if cached is not None and (time.monotonic() - cached[0]) < CLAIM_TTL_SECONDS:
        return cached[1]
    if not mind_id:
        return ""
    url = f"{SERVER_URL}/broker/minds/{mind_id}/surface-claim?surface={surface}"
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(
                url, headers=GATEWAY_AUTH_HEADERS,
                timeout=aiohttp.ClientTimeout(total=8.0),
            ) as response:
                if response.status >= 400:
                    # A gateway that predates the claim routes, one that
                    # refuses this credential, one that is down: the
                    # stack-wide chat is what every one of these had before,
                    # so that is what they keep. No branch per status,
                    # because there is nothing different to do.
                    log.warning(
                        "claim read for %s answered %s; using the operator chat",
                        mind_id, response.status,
                    )
                    return ""
                body = await response.json()
    except Exception as exc:  # noqa: BLE001 — a delivery address never raises
        log.warning("could not read the claim for %s: %s", mind_id, exc)
        return ""
    ref = str((body or {}).get("claimed_ref") or "")
    if ref:
        _claimed_chats[key] = (time.monotonic(), ref)
    # An unclaimed bot is not cached either. It is the state a mind passes
    # through between its container starting and its claim being adopted, and
    # caching it holds this mind on the stack-wide chat for the window — which
    # is another person's reminder delivered to the operator, the one thing
    # the empty answer exists to prevent.
    return ref


async def owner_chat_for(mind_id: str, surface: str = "telegram") -> int:
    """The claimed chat for this mind, falling back to the stack-wide one.

    The fallback is for a gateway with no claim routes and for a mind whose
    bot nobody has claimed yet — both of which are states the stack-wide
    chat already served, so nothing stops being delivered by this changing.
    """
    ref = await claimed_chat_id(mind_id, surface)
    if ref.isdigit() or (ref.startswith("-") and ref[1:].isdigit()):
        return int(ref)
    return config.telegram_owner_chat_id


async def _report_gate_failure(
    label: str, reason: str, mind_id: str = ""
) -> None:
    """Tell Daniel a gate is broken. Telegram, not the task's own channel —
    the channel is for what the task has to say, and a gate that never runs
    has nothing to say there.
    """
    log_event(log, "scheduled_skill.gate_failed", task=label, reason=reason)
    log.error("Gate failed for %s — %s", label, reason)
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        return
    try:
        await _deliver_text(
            mind_id, bot_token,
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


def _build_voice_servers():
    """Resolves a mind to the voice server that speaks for it.

    From the mind's own record rather than from this process's environment: a
    host running both engines would otherwise speak every scheduled run
    through whichever one `VOICE_SERVER_URL` happens to name.

    Imported defensively because the surfaces core is a git dependency that an
    image can be built without — and a scheduler that refused to start over a
    routing improvement would take every briefing and reminder down with it.
    A host without the package keeps the single server it has always used.
    """
    try:
        from hive_surfaces import voice_routing
    except ImportError:
        log.warning(
            "hive_surfaces is not installed; scheduled speech stays on %s",
            VOICE_SERVER_URL,
        )
        return None
    return voice_routing.VoiceServerResolver(
        os.environ.get("COMMS_URL", ""),
        os.environ.get("COMMS_BEARER_TOKEN", ""),
        {
            voice_routing.CHATTERBOX: os.environ.get(
                "VOICE_SERVER_URL_CHATTERBOX", ""
            ),
            voice_routing.KOKORO: os.environ.get("VOICE_SERVER_URL_KOKORO", ""),
        },
        fallback_url=VOICE_SERVER_URL,
    )


_voice_servers = _build_voice_servers()


def _voice_server_for(voice_id: str) -> str:
    """The server this mind's record points at, or the one URL we hold."""
    if _voice_servers is None:
        return VOICE_SERVER_URL
    return _voice_servers.resolve(voice_id) or VOICE_SERVER_URL


async def _tts(http: aiohttp.ClientSession, text: str, voice_id: str) -> bytes:
    timeout = aiohttp.ClientTimeout(total=VOICE_TTS_TIMEOUT_SECONDS)
    async with http.post(
        f"{_voice_server_for(voice_id)}/tts",
        json={"text": text, "voice_id": voice_id},
        timeout=timeout,
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"TTS error {resp.status}: {await resp.text()}")
        return await resp.read()


async def _try_send_voice(bot_token: str, chat_id: int, text: str, voice_id: str, label: str) -> None:
    """Fire-and-forget: synthesise TTS and send the voice note.

    Takes the chat the text was actually delivered to rather than resolving
    the claim again. The claim is read across a cache, so a second lookup can
    answer differently inside one fire — which put the words in one person's
    chat and the audio in another's.

    Logs but never raises.
    """
    try:
        async with aiohttp.ClientSession() as http:
            audio = await _tts(http, text, voice_id)
        if not await _send_voice(bot_token, chat_id, audio):
            log_event(
                log, "scheduled_skill.voice_refused", level=logging.ERROR,
                task=label, chat_id=str(chat_id),
            )
            return
        log.info("Voice delivery complete for %s", label)
    except Exception:
        log.exception("Voice delivery failed for %s (text already sent)", label)


#: Where Telegram's bot API lives. A constant rather than a literal in each
#: call so the two send paths can be driven against a stand-in: what matters
#: about them is the status they read back, and that is the one thing a test
#: cannot observe through the real endpoint.
TELEGRAM_API_BASE = "https://api.telegram.org"


async def _send_voice(bot_token: str, chat_id: int, audio: bytes) -> bool:
    """Deliver the audio, and say whether Telegram took it.

    Read rather than discarded, for the reason `_send_text` reads its own: a
    bot the target has never started refuses `sendVoice` exactly as it
    refuses `sendMessage`, and a discarded response made that a reminder
    heard by nobody with nothing anywhere saying so.
    """
    form = aiohttp.FormData()
    form.add_field("voice", audio, filename="response.ogg", content_type="audio/ogg")
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{TELEGRAM_API_BASE}/bot{bot_token}/sendVoice",
            params={"chat_id": str(chat_id)},
            data=form,
        ) as response:
            return response.status < 400


async def _send_text(bot_token: str, chat_id: int, text: str) -> bool:
    """Deliver `text`, and say whether Telegram took it.

    The answer is read rather than discarded. This process holds one bot
    token and now addresses whichever chat claimed the raising mind's *own*
    bot — and Telegram refuses `sendMessage` from a bot the target has never
    started. Dropping the response made that a reminder nobody receives and
    nobody can see was missed.
    """
    limit = 4096
    delivered = True
    for i in range(0, max(len(text), 1), limit):
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage",
                json={"chat_id": str(chat_id), "text": text[i : i + limit]},
            ) as response:
                if response.status >= 400:
                    body = (await response.text())[:300]
                    log_event(
                        log, "scheduled_skill.delivery_refused",
                        level=logging.ERROR, chat_id=str(chat_id),
                        status=response.status, detail=body,
                    )
                    delivered = False
    return delivered


async def _deliver_text(
    mind_id: str, bot_token: str, text: str, *, chat_id: int | None = None
) -> int:
    """Deliver to the mind's own person, falling back to the operator.

    A reminder belongs to whoever asked for it. But the bot this process
    holds may have no conversation with that person — their mind's bot is a
    different bot — and a refusal there must not end as a reminder that
    simply never arrives. So a refused delivery is logged and repeated to the
    stack-wide chat, which is where every one of these went before.

    Answers with the chat the words actually reached, or zero if none did, so
    anything following them lands in the same place rather than resolving the
    claim a second time.
    """
    if chat_id is None:
        chat_id = await owner_chat_for(mind_id)
    if chat_id and await _send_text(bot_token, chat_id, text):
        return chat_id
    fallback = config.telegram_owner_chat_id
    if not fallback or fallback == chat_id:
        return 0
    log_event(
        log, "scheduled_skill.delivery_fell_back", level=logging.WARNING,
        mind_id=mind_id, intended=str(chat_id), delivered_to=str(fallback),
    )
    if await _send_text(bot_token, fallback, text):
        return fallback
    return 0


async def _create_session(http: aiohttp.ClientSession, skill: ScheduledSkill, surface_prompt: str) -> str:
    """Create a fresh session for this fire. Returns session id."""
    client_ref = f"scheduler-{skill.mind_name}-{skill.skill_name}-{uuid.uuid4().hex[:8]}"
    payload = {
        "owner_type": "scheduler",
        "owner_ref": str(await owner_chat_for(skill.mind_id) or "scheduler"),
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
    (`discord:<mind_id>`, channel_id), create and bind only when there is
    none. The bot namespaces its client type by mind so two minds' bots in
    one channel keep separate conversations, and the key here has to be
    that one: any other addressing mints a second session for a channel
    that already had one, so every fire posts into a conversation Daniel's
    replies never reach.
    """
    client_type = f"discord:{skill.mind_id}"
    async with http.get(
        f"{SERVER_URL}/sessions",
        params={"client_type": client_type, "client_ref": channel_id},
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

    # The mind's own Discord claim, since one bot answers one person. The
    # old reading took the first entry of a stack-wide allow-list, which is
    # whoever happened to be listed first on the whole machine.
    owner_ref = str(
        await claimed_chat_id(skill.mind_id, "discord")
        or (config.discord_allowed_users[0] if config.discord_allowed_users else "")
        or "scheduler"
    )
    payload = {
        "owner_type": client_type,
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
            mind_id=skill.mind_id,
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
            await _report_gate_failure(label, reason, mind_id=skill.mind_id)
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
    chat_id = await owner_chat_for(skill.mind_id)

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

    # The chat resolved once at the top of this fire, not resolved again
    # here: the claim is read across a cache that no longer holds an empty
    # answer, so two lookups in one fire genuinely can disagree — which put
    # one person's reminder in another person's chat, one branch over from
    # the voice note that had the same bug.
    delivered_to = await _deliver_text(
        skill.mind_id, bot_token, response, chat_id=chat_id
    )
    if skill.voice and delivered_to:
        asyncio.create_task(
            _try_send_voice(bot_token, delivered_to, response, skill.mind_id, label)
        )


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
