"""Tests for scheduled tasks that live in a Discord channel.

Each test names the requirement it protects. The requirements are in
`docs/architecture/channel-resident-scheduled-tasks.md`.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import discord

from bots import discord_bot, scheduler
from core import discord_delivery
from core.scheduled_skills import ScheduledSkill, discover_scheduled_skills


@pytest.fixture(autouse=True)
def _fresh_task_channel_cache():
    """The bot holds its discovery briefly so it is not walked per message.
    That cache is module state, so it outlives a test unless cleared."""
    discord_bot._task_channel_cache.clear()
    yield
    discord_bot._task_channel_cache.clear()


class _AsyncCtx:
    def __init__(self, ret):
        self._ret = ret

    async def __aenter__(self):
        return self._ret

    async def __aexit__(self, *_):
        return False


def _skill(**overrides) -> ScheduledSkill:
    base = dict(
        mind_id="ada-uuid",
        mind_name="ada",
        skill_name="7am",
        skill_path="/minds/ada/.claude/skills/7am/SKILL.md",
        cron="0 7 * * *",
        timezone="America/Chicago",
        voice=False,
        notify=True,
    )
    base.update(overrides)
    return ScheduledSkill(**base)


def _write_skill(minds_root: Path, name: str, body: str) -> None:
    d = minds_root / "ada" / ".claude" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body)
    (minds_root / "ada" / "runtime.yaml").write_text("mind_id: ada-uuid\n")


# ---------------------------------------------------------------------------
# R1/R2 — a task naming a channel delivers there; one naming none uses Telegram
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_task_with_channel_posts_to_discord_and_not_telegram():
    skill = _skill(discord_channel="5551212")
    with patch.object(scheduler, "_fire_into_channel", new=AsyncMock()) as into_channel, \
         patch.object(scheduler, "_send_text", new=AsyncMock()) as telegram:
        await scheduler.fire_skill(skill)

    into_channel.assert_awaited_once_with(skill, "5551212")
    telegram.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_without_channel_still_delivers_over_telegram():
    skill = _skill(discord_channel=None)
    with patch.object(scheduler, "_fire_into_channel", new=AsyncMock()) as into_channel, \
         patch.object(scheduler, "_create_session", new=AsyncMock(return_value="sid-1")), \
         patch.object(scheduler, "_send_message", new=AsyncMock(return_value="the briefing")), \
         patch.object(scheduler, "_kill_session", new=AsyncMock()), \
         patch.object(scheduler, "_send_text", new=AsyncMock()) as telegram, \
         patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok"}), \
         patch.object(scheduler.config, "telegram_owner_chat_id", 99):
        await scheduler.fire_skill(skill)

    into_channel.assert_not_awaited()
    telegram.assert_awaited_once()
    assert telegram.await_args.args[2] == "the briefing"


# ---------------------------------------------------------------------------
# R6 — what is posted is the briefing, not the nudge that triggered it
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_channel_receives_the_response_not_the_nudge():
    posted = {}

    async def fake_post(http, token, channel_id, text):
        posted["channel_id"] = channel_id
        posted["text"] = text

    with patch.object(scheduler, "_ensure_channel_session", new=AsyncMock(return_value="sid-1")), \
         patch.object(scheduler, "_send_message", new=AsyncMock(return_value="your day ahead")) as send, \
         patch.object(discord_delivery, "bot_token", return_value="bot-tok"), \
         patch.object(discord_delivery, "post_to_channel", new=fake_post):
        await scheduler._fire_into_channel(_skill(discord_channel="777"), "777")

    nudge = send.await_args.args[2]
    assert "7am" in nudge
    assert posted["text"] == "your day ahead"
    assert posted["channel_id"] == "777"


def test_the_nudge_names_the_skill_and_carries_nothing_else():
    """Requirement 6: the fire carries no instructions. A nudge that grew to
    carry the skill body would put a copy of it in the conversation daily."""
    skill = _skill(skill_path="/minds/ada/.claude/skills/7am/SKILL.md")
    nudge = scheduler._nudge(skill)

    assert "7am" in nudge
    assert skill.skill_path not in nudge
    assert len(nudge) < 120


@pytest.mark.asyncio
async def test_the_fire_leaves_the_conversation_alive():
    """Requirement 3: the session outlives the fire. Killing it would restore
    the one-shot behaviour with a Discord skin, and every other assertion in
    this file would still hold."""
    with patch.object(scheduler, "_ensure_channel_session", new=AsyncMock(return_value="sid")), \
         patch.object(scheduler, "_send_message", new=AsyncMock(return_value="briefing")), \
         patch.object(scheduler, "_kill_session", new=AsyncMock()) as kill, \
         patch.object(discord_delivery, "bot_token", return_value="tok"), \
         patch.object(discord_delivery, "post_to_channel", new=AsyncMock()):
        await scheduler._fire_into_channel(_skill(discord_channel="777"), "777")

    kill.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_gateway_failure_posts_nothing_to_the_channel():
    """Requirement 9: a fire that never produced a briefing does not put an
    error string in front of Daniel dressed as one."""
    with patch.object(scheduler, "_ensure_channel_session",
                      new=AsyncMock(side_effect=RuntimeError("gateway down"))), \
         patch.object(discord_delivery, "bot_token", return_value="tok"), \
         patch.object(discord_delivery, "post_to_channel", new=AsyncMock()) as post:
        await scheduler._fire_into_channel(_skill(discord_channel="777"), "777")

    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_token_means_no_call_to_discord():
    """Requirement 9: an unauthenticated post cannot succeed, so it is not
    attempted — an empty bearer would 401 once per day forever."""
    with patch.object(discord_delivery, "bot_token", return_value=""), \
         patch.object(scheduler, "_ensure_channel_session", new=AsyncMock()) as ensure, \
         patch.object(discord_delivery, "post_to_channel", new=AsyncMock()) as post:
        await scheduler._fire_into_channel(_skill(discord_channel="777"), "777")

    ensure.assert_not_awaited()
    post.assert_not_awaited()


# ---------------------------------------------------------------------------
# R3/R8 — the fire reuses the channel's session, or creates and binds one
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_existing_channel_session_is_reused():
    listing = MagicMock()
    listing.status = 200
    listing.json = AsyncMock(return_value=[
        {"id": "old-sid", "is_active": False},
        {"id": "live-sid", "is_active": True},
    ])
    http = MagicMock()
    http.get = MagicMock(return_value=_AsyncCtx(listing))
    http.post = MagicMock()

    sid = await scheduler._ensure_channel_session(http, _skill(), "777")

    assert sid == "live-sid"
    http.post.assert_not_called()
    _, kwargs = http.get.call_args
    assert kwargs["params"] == {"client_type": "discord", "client_ref": "777"}


@pytest.mark.asyncio
async def test_channel_with_no_session_gets_one_bound_to_it():
    listing = MagicMock()
    listing.status = 200
    listing.json = AsyncMock(return_value=[])
    created = MagicMock()
    created.json = AsyncMock(return_value={"id": "new-sid"})

    http = MagicMock()
    http.get = MagicMock(return_value=_AsyncCtx(listing))
    http.post = MagicMock(return_value=_AsyncCtx(created))

    with patch.object(scheduler.config, "discord_allowed_users", [4242]):
        sid = await scheduler._ensure_channel_session(http, _skill(), "777")

    assert sid == "new-sid"
    _, kwargs = http.post.call_args
    assert kwargs["json"]["owner_type"] == "discord"
    assert kwargs["json"]["client_ref"] == "777"
    assert kwargs["json"]["owner_ref"] == "4242"
    assert kwargs["json"]["mind_id"] == "ada-uuid"


# ---------------------------------------------------------------------------
# R4 — a task channel needs no mention; an ordinary channel still does
# ---------------------------------------------------------------------------
def test_task_channel_message_is_handled_without_a_mention():
    assert discord_bot.should_handle_message(
        is_dm=False, mentioned=False, channel_id=777, task_channel_ids={777}
    ) is True


def test_ordinary_channel_message_without_a_mention_is_ignored():
    assert discord_bot.should_handle_message(
        is_dm=False, mentioned=False, channel_id=888, task_channel_ids={777}
    ) is False
    assert discord_bot.should_handle_message(
        is_dm=False, mentioned=True, channel_id=888, task_channel_ids={777}
    ) is True


@pytest.mark.asyncio
async def test_a_task_channel_is_not_blocked_by_the_channel_allowlist():
    """Requirement 4: the skill naming a channel is what admits it. A task
    channel missing from `discord_allowed_channels` would otherwise receive
    briefings nobody is allowed to answer."""
    handled = {}

    message = MagicMock()
    message.author.id = 4242
    message.channel = MagicMock(spec=discord.TextChannel)
    message.channel.id = 777
    message.content = "push that to tomorrow"
    message.mentions = []

    async def fake_stream(sent, user_id, channel_id, prompt):
        handled["channel_id"] = channel_id
        handled["prompt"] = prompt
        return "ok"

    with patch.object(discord_bot, "task_channels", return_value={777}), \
         patch.object(discord_bot.config, "discord_allowed_users", [4242]), \
         patch.object(discord_bot.config, "discord_allowed_channels", [999]), \
         patch.object(discord_bot, "_stream_to_message", new=fake_stream), \
         patch.object(discord_bot, "_play_tts_for_member", new=AsyncMock()), \
         patch.object(discord_bot, "bot", MagicMock(user=MagicMock(id=1))):
        discord_bot.bot.user.__eq__ = lambda self, other: False
        message.reply = AsyncMock(return_value=MagicMock())
        await discord_bot.on_message(message)

    assert handled["channel_id"] == 777
    assert handled["prompt"] == "push that to tomorrow"


def test_a_channel_claimed_by_another_mind_is_not_this_mind_s(tmp_path):
    """Requirement 4: one skills root holds every mind. A channel another
    mind claims must not become mention-free here, or this bot answers into
    a thread it was never given the skill for."""
    _write_skill(tmp_path, "7am", (
        '---\nname: 7am\nschedule: "0 7 * * *"\ndiscord_channel: "777"\n---\nbody\n'
    ))

    assert discord_bot.task_channels(tmp_path, "ada-uuid") == {777}
    assert discord_bot.task_channels(tmp_path, "someone-else") == set()


def test_a_whitespace_only_response_is_not_a_briefing():
    """Requirement 9: Discord rejects empty content, so a blank turn has to
    fail here rather than arrive as a 400 that reads like a delivery bug."""
    import asyncio

    resp = MagicMock()
    resp.status = 200

    class _Content:
        async def iter_any(self):
            yield b'data: {"type": "result", "result": "   "}\n'

    resp.content = _Content()
    http = MagicMock()
    http.post = MagicMock(return_value=_AsyncCtx(resp))

    with pytest.raises(RuntimeError, match="Empty response"):
        asyncio.run(scheduler._send_message(http, "sid", "nudge"))


def test_a_thread_under_a_task_channel_stays_in_that_conversation():
    """Requirement 3: a threaded reply is about the briefing above it, so it
    belongs to the channel's conversation rather than opening a second one
    holding none of it."""
    thread = MagicMock()
    thread.id = 9001
    thread.parent_id = 777

    assert discord_bot.conversation_channel_id(thread, {777}) == 777


def test_a_thread_in_an_ordinary_channel_keeps_its_own_conversation():
    thread = MagicMock()
    thread.id = 9001
    thread.parent_id = 888

    assert discord_bot.conversation_channel_id(thread, {777}) == 9001


def test_a_skill_that_cannot_be_decoded_does_not_take_discovery_down(tmp_path):
    """Requirement 4: discovery runs on the bot's inbound path, so a single
    badly-encoded skill file raising there makes the bot deaf everywhere."""
    _write_skill(tmp_path, "7am", (
        '---\nname: 7am\nschedule: "0 7 * * *"\ndiscord_channel: "777"\n---\nbody\n'
    ))
    bad = tmp_path / "ada" / ".claude" / "skills" / "broken"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_bytes(
        b'---\nname: broken\nschedule: "0 8 * * *"\n---\nCaf\xe9\n'
    )

    assert discord_bot.task_channels(tmp_path) == {777}


def test_task_channels_are_read_from_the_skills(tmp_path):
    _write_skill(tmp_path, "7am", (
        '---\nname: 7am\nschedule: "0 7 * * *"\ndiscord_channel: "777"\n---\nbody\n'
    ))
    _write_skill(tmp_path, "1pm", (
        '---\nname: 1pm\nschedule: "0 13 * * *"\n---\nbody\n'
    ))

    assert discord_bot.task_channels(tmp_path) == {777}


def test_a_digit_that_is_not_an_ascii_digit_is_not_a_channel_id(tmp_path):
    """Requirement 4: `isdigit` is true of superscripts and fullwidth forms,
    which either raise on int() or address a channel that does not exist."""
    _write_skill(tmp_path, "7am", (
        '---\nname: 7am\nschedule: "0 7 * * *"\ndiscord_channel: "\u00b2"\n---\nbody\n'
    ))

    assert [s.discord_channel for s in discover_scheduled_skills(tmp_path)] == [None]


def test_a_channel_name_is_not_taken_as_a_channel_id(tmp_path):
    _write_skill(tmp_path, "7am", (
        '---\nname: 7am\nschedule: "0 7 * * *"\ndiscord_channel: "#schedule"\n---\nbody\n'
    ))

    discovered = discover_scheduled_skills(tmp_path)

    assert [s.discord_channel for s in discovered] == [None]


# ---------------------------------------------------------------------------
# R5 — voice rides along when it synthesises, and its failure costs nothing
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_successful_synthesis_is_posted_to_the_channel():
    with patch.object(scheduler, "_ensure_channel_session", new=AsyncMock(return_value="sid")), \
         patch.object(scheduler, "_send_message", new=AsyncMock(return_value="briefing")), \
         patch.object(scheduler, "_tts", new=AsyncMock(return_value=b"OGG")), \
         patch.object(discord_delivery, "bot_token", return_value="tok"), \
         patch.object(discord_delivery, "post_to_channel", new=AsyncMock()), \
         patch.object(discord_delivery, "post_audio", new=AsyncMock()) as post_audio:
        await scheduler._fire_into_channel(_skill(voice=True, discord_channel="777"), "777")

    post_audio.assert_awaited_once()
    assert post_audio.await_args.args[3] == b"OGG"


@pytest.mark.asyncio
async def test_failed_synthesis_still_posts_the_text():
    seen = {}

    async def fake_post(http, token, channel_id, text):
        seen["text"] = text

    with patch.object(scheduler, "_ensure_channel_session", new=AsyncMock(return_value="sid")), \
         patch.object(scheduler, "_send_message", new=AsyncMock(return_value="briefing")), \
         patch.object(scheduler, "_tts", new=AsyncMock(side_effect=RuntimeError("tts down"))), \
         patch.object(discord_delivery, "bot_token", return_value="tok"), \
         patch.object(discord_delivery, "post_audio", new=AsyncMock()) as post_audio, \
         patch.object(discord_delivery, "post_to_channel", new=fake_post):
        await scheduler._fire_into_channel(_skill(voice=True, discord_channel="777"), "777")

    assert seen["text"] == "briefing"
    post_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_refused_attachment_does_not_cost_the_briefing():
    """Requirement 5: Attach Files is its own per-channel permission, so a
    channel that takes text but refuses files must still deliver."""
    seen = {}

    async def fake_post(http, token, channel_id, text):
        seen["text"] = text

    events = []

    with patch.object(scheduler, "_ensure_channel_session", new=AsyncMock(return_value="sid")), \
         patch.object(scheduler, "_send_message", new=AsyncMock(return_value="briefing")), \
         patch.object(scheduler, "_tts", new=AsyncMock(return_value=b"OGG")), \
         patch.object(discord_delivery, "bot_token", return_value="tok"), \
         patch.object(discord_delivery, "post_to_channel", new=fake_post), \
         patch.object(discord_delivery, "post_audio",
                      new=AsyncMock(side_effect=RuntimeError("HTTP 403"))), \
         patch.object(scheduler, "log_event",
                      new=lambda _l, event, **f: events.append((event, f))):
        await scheduler._fire_into_channel(_skill(voice=True, discord_channel="777"), "777")

    assert seen["text"] == "briefing"
    completed = [f for event, f in events if event == "scheduled_skill.completed"]
    assert completed and completed[0]["voice"] is False


@pytest.mark.asyncio
async def test_an_unreadable_session_listing_does_not_mint_a_rival_session():
    """Requirements 3 and 8: a listing that could not be read is not the same
    answer as a channel with no session. Creating one rebinds the channel and
    orphans every previous briefing, and the fire would report success."""
    listing = MagicMock()
    listing.status = 503
    listing.json = AsyncMock(return_value=[])
    http = MagicMock()
    http.get = MagicMock(return_value=_AsyncCtx(listing))
    http.post = MagicMock()

    with pytest.raises(RuntimeError, match="503"):
        await scheduler._ensure_channel_session(http, _skill(), "777")

    http.post.assert_not_called()


# ---------------------------------------------------------------------------
# R9 — a refused post is a failure, never a completion
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_rejected_post_raises_rather_than_returning():
    resp = MagicMock()
    resp.status = 403
    resp.text = AsyncMock(return_value="Missing Permissions")
    http = MagicMock()
    http.post = MagicMock(return_value=_AsyncCtx(resp))

    with pytest.raises(RuntimeError, match="403"):
        await discord_delivery.post_to_channel(http, "tok", "777", "briefing")


@pytest.mark.asyncio
async def test_a_failed_delivery_is_not_logged_as_completed():
    events = []

    def capture(_log, event, **fields):
        events.append(event)

    with patch.object(scheduler, "_ensure_channel_session", new=AsyncMock(return_value="sid")), \
         patch.object(scheduler, "_send_message", new=AsyncMock(return_value="briefing")), \
         patch.object(discord_delivery, "bot_token", return_value="tok"), \
         patch.object(discord_delivery, "post_to_channel",
                      new=AsyncMock(side_effect=RuntimeError("HTTP 403"))), \
         patch.object(scheduler, "log_event", new=capture):
        await scheduler._fire_into_channel(_skill(discord_channel="777"), "777")

    assert "scheduled_skill.delivery_failed" in events
    assert "scheduled_skill.completed" not in events


# ---------------------------------------------------------------------------
# Delivery mechanics the requirements depend on
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_post_is_addressed_to_the_channel_and_signed_as_the_bot():
    """Requirements 1 and 9: the two things the delivery call has to get
    right are where it goes and who it claims to be."""
    resp = MagicMock()
    resp.status = 200
    http = MagicMock()
    http.post = MagicMock(return_value=_AsyncCtx(resp))

    await discord_delivery.post_to_channel(http, "bot-tok", "777", "briefing")

    args, kwargs = http.post.call_args
    assert args[0] == "https://discord.com/api/v10/channels/777/messages"
    assert kwargs["headers"]["Authorization"] == "Bot bot-tok"


def test_an_empty_briefing_is_still_a_message():
    """Requirement 9: nothing posted and a post that failed must not be the
    same outcome, so an empty response still produces one call."""
    assert discord_delivery.split_message("") == [""]


def test_the_token_falls_back_to_the_environment():
    """Requirement 1: a process with no keyring still delivers."""
    with patch.dict("os.environ", {"DISCORD_BOT_TOKEN": "env-tok"}), \
         patch.dict("sys.modules", {"keyring": None}):
        assert discord_delivery.bot_token() == "env-tok"


@pytest.mark.asyncio
async def test_a_long_briefing_is_split_rather_than_refused():
    text = "x" * 4500
    resp = MagicMock()
    resp.status = 200
    http = MagicMock()
    http.post = MagicMock(return_value=_AsyncCtx(resp))

    await discord_delivery.post_to_channel(http, "tok", "777", text)

    sent = [kw["json"]["content"] for _, kw in http.post.call_args_list]
    assert [len(chunk) for chunk in sent] == [2000, 2000, 500]
    assert "".join(sent) == text


@pytest.mark.asyncio
async def test_a_rate_limited_chunk_is_retried_rather_than_abandoned():
    """Requirement 1: a long briefing is several posts in a row and Discord
    allows five per five seconds, so a 429 partway through must not leave a
    briefing that stops mid-sentence."""
    limited = MagicMock()
    limited.status = 429
    limited.json = AsyncMock(return_value={"retry_after": 0.01})
    ok = MagicMock()
    ok.status = 200

    http = MagicMock()
    http.post = MagicMock(side_effect=[_AsyncCtx(limited), _AsyncCtx(ok)])

    await discord_delivery.post_to_channel(http, "tok", "777", "briefing")

    assert http.post.call_count == 2


@pytest.mark.asyncio
async def test_a_persistent_rate_limit_is_finally_reported():
    limited = MagicMock()
    limited.status = 429
    limited.json = AsyncMock(return_value={"retry_after": 0.001})
    limited.text = AsyncMock(return_value="rate limited")

    http = MagicMock()
    http.post = MagicMock(return_value=_AsyncCtx(limited))

    with pytest.raises(RuntimeError, match="429"):
        await discord_delivery.post_to_channel(http, "tok", "777", "briefing")


@pytest.mark.asyncio
async def test_audio_is_its_own_message():
    resp = MagicMock()
    resp.status = 200
    http = MagicMock()
    http.post = MagicMock(return_value=_AsyncCtx(resp))

    await discord_delivery.post_audio(http, "tok", "777", b"OGG")

    args, kwargs = http.post.call_args
    assert args[0] == "https://discord.com/api/v10/channels/777/messages"
    assert "data" in kwargs
    assert kwargs["headers"]["Authorization"] == "Bot tok"
