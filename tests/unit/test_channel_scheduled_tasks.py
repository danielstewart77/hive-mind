"""Tests for scheduled tasks that live in a Discord channel.

Each test names the requirement it protects. The requirements are in
`docs/architecture/channel-resident-scheduled-tasks.md`.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bots import discord_bot, scheduler
from core import discord_delivery
from core.scheduled_skills import ScheduledSkill, discover_scheduled_skills


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

    async def fake_post(http, token, channel_id, text, audio=None, **kw):
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


def test_task_channels_are_read_from_the_skills(tmp_path):
    _write_skill(tmp_path, "7am", (
        '---\nname: 7am\nschedule: "0 7 * * *"\ndiscord_channel: "777"\n---\nbody\n'
    ))
    _write_skill(tmp_path, "1pm", (
        '---\nname: 1pm\nschedule: "0 13 * * *"\n---\nbody\n'
    ))

    assert discord_bot.task_channels(tmp_path) == {777}


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
async def test_successful_synthesis_is_attached_to_the_post():
    seen = {}

    async def fake_post(http, token, channel_id, text, audio=None, **kw):
        seen["audio"] = audio

    with patch.object(scheduler, "_ensure_channel_session", new=AsyncMock(return_value="sid")), \
         patch.object(scheduler, "_send_message", new=AsyncMock(return_value="briefing")), \
         patch.object(scheduler, "_tts", new=AsyncMock(return_value=b"OGG")), \
         patch.object(discord_delivery, "bot_token", return_value="tok"), \
         patch.object(discord_delivery, "post_to_channel", new=fake_post):
        await scheduler._fire_into_channel(_skill(voice=True, discord_channel="777"), "777")

    assert seen["audio"] == b"OGG"


@pytest.mark.asyncio
async def test_failed_synthesis_still_posts_the_text():
    seen = {}

    async def fake_post(http, token, channel_id, text, audio=None, **kw):
        seen["text"] = text
        seen["audio"] = audio

    with patch.object(scheduler, "_ensure_channel_session", new=AsyncMock(return_value="sid")), \
         patch.object(scheduler, "_send_message", new=AsyncMock(return_value="briefing")), \
         patch.object(scheduler, "_tts", new=AsyncMock(side_effect=RuntimeError("tts down"))), \
         patch.object(discord_delivery, "bot_token", return_value="tok"), \
         patch.object(discord_delivery, "post_to_channel", new=fake_post):
        await scheduler._fire_into_channel(_skill(voice=True, discord_channel="777"), "777")

    assert seen["text"] == "briefing"
    assert seen["audio"] is None


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
async def test_audio_rides_on_the_final_chunk():
    resp = MagicMock()
    resp.status = 200
    http = MagicMock()
    http.post = MagicMock(return_value=_AsyncCtx(resp))

    await discord_delivery.post_to_channel(http, "tok", "777", "y" * 2500, audio=b"OGG")

    calls = http.post.call_args_list
    assert "json" in calls[0][1]
    assert "data" in calls[1][1]
