"""`/effort` offers the levels the conversation's model takes, as buttons.

The levels come from the gateway, which asked the mind, which asked the
proxy — so these tests drive the bot with the gateway's answer and assert on
what the operator would see and what the bot sends back.
"""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import hive_surfaces.discord_bot as db
import hive_surfaces.telegram_bot as tb
from hive_surfaces import effort_picker, model_picker, session_picker
from hive_surfaces.config import SurfaceConfig, config, configure

LEVELS = {"model": "claude-opus-5", "levels": ["low", "medium", "high", "max"],
          "current": "high"}


@pytest.fixture(autouse=True)
def _restore_config():
    before = config._installed()
    configure(SurfaceConfig(telegram_allowed_users=[123]))
    yield
    configure(before)


def _update(args=None):
    update = MagicMock()
    update.effective_user.id = 123
    update.effective_chat.id = 456
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = args or []
    context.bot.send_message = AsyncMock()
    return update, context


def _labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def _payloads(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _tap(level: str, message_id: int = 99):
    query = MagicMock()
    query.data = effort_picker.encode(level)
    query.answer = AsyncMock()
    query.message.message_id = message_id
    query.message.text = "Effort for claude-opus-5:"
    query.edit_message_reply_markup = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    update.effective_user.id = 123
    update.effective_chat.id = 456
    context = MagicMock()
    context.bot = MagicMock()
    return update, context


class TestTheKeyboard:
    @pytest.mark.asyncio
    async def test_a_bare_effort_draws_one_button_per_level_with_the_current_ticked(self):
        update, context = _update()
        asked = AsyncMock(return_value=LEVELS)

        with patch.object(tb, "gateway", MagicMock(server_command=asked)):
            await tb.cmd_effort(update, context)

        assert asked.await_args.args[2] == "/effort"
        markup = context.bot.send_message.await_args.kwargs["reply_markup"]
        assert _payloads(markup) == [effort_picker.encode(lv) for lv in LEVELS["levels"]]
        assert [lb for lb in _labels(markup) if "✓" in lb] == ["high ✓"]

    @pytest.mark.asyncio
    async def test_a_model_taking_no_effort_gets_a_sentence_and_no_keyboard(self):
        update, context = _update()

        with patch.object(tb, "gateway", MagicMock(server_command=AsyncMock(
                    return_value={"model": "qwen35-131k", "levels": [], "current": None}))), \
                patch.object(tb, "_deliver", new=AsyncMock()) as delivered:
            await tb.cmd_effort(update, context)

        assert context.bot.send_message.await_count == 0
        assert "qwen35-131k takes no effort" in delivered.await_args.args[2]

    @pytest.mark.asyncio
    async def test_a_gateway_refusal_is_reported_rather_than_read_as_no_levels(self):
        """"Couldn't read the model list" must not become "takes no effort"."""
        update, context = _update()

        with patch.object(tb, "gateway", MagicMock(server_command=AsyncMock(
                    return_value={"error": "Couldn't read which models this mind offers"}))), \
                patch.object(tb, "_deliver", new=AsyncMock()) as delivered:
            await tb.cmd_effort(update, context)

        assert "Couldn't read" in delivered.await_args.args[2]
        assert "no effort" not in delivered.await_args.args[2]

    @pytest.mark.asyncio
    async def test_a_typed_level_is_set_without_drawing_a_keyboard(self):
        update, context = _update(args=["max"])

        with patch.object(tb, "_handle_server_command",
                          new=AsyncMock(return_value="Effort set to max")) as sent:
            await tb.cmd_effort(update, context)

        assert sent.await_args.args[0] == "/effort max"
        assert context.bot.send_message.await_count == 0

    def test_a_set_reports_the_level_the_gateway_recorded(self):
        assert effort_picker.format_effort_result({"effort": "max"}) == "Effort set to max"
        assert "No effort change" in effort_picker.format_effort_result({})


class TestTappingALevel:
    @pytest.mark.asyncio
    async def test_a_tapped_level_is_the_level_set(self):
        with patch.object(tb, "_handle_server_command",
                          new=AsyncMock(return_value="Effort set to low")) as sent:
            worked, _ = await tb._run_session_button(effort_picker.CB_EFFORT, "low", 123, 456)

        assert sent.await_args.args[0] == "/effort low"
        assert worked is True

    @pytest.mark.asyncio
    async def test_a_refused_level_is_reported_as_a_failure(self):
        with patch.object(tb, "_handle_server_command",
                          new=AsyncMock(return_value="Error: This conversation is mid-answer.")):
            worked, msg = await tb._run_session_button(effort_picker.CB_EFFORT, "low", 123, 456)

        assert worked is False
        assert "mid-answer" in msg

    @pytest.mark.asyncio
    async def test_a_second_tap_on_one_effort_picker_sets_nothing(self, tmp_path, monkeypatch):
        from hive_surfaces import bot_utils

        monkeypatch.setenv("PICKER_STATE_PATH", str(tmp_path / "spent_pickers.json"))
        bot_utils._reset_pickers()
        try:
            with patch.object(tb, "_handle_server_command",
                              new=AsyncMock(return_value="Effort set to low")) as sent, \
                    patch.object(tb, "_deliver", new=AsyncMock()):
                for _ in range(2):
                    await tb.on_session_button(*_tap("low"))
        finally:
            bot_utils._reset_pickers()

        assert sent.await_count == 1

    def test_the_running_application_routes_an_effort_tap_to_a_handler(self):
        from telegram.ext import CallbackQueryHandler

        with patch.object(tb, "_on_startup"), patch.object(tb, "_on_shutdown"):
            app = tb._build_application("123456:fake-token-for-tests")
        payload = effort_picker.encode("high")

        routed = [
            h for group in app.handlers.values() for h in group
            if isinstance(h, CallbackQueryHandler)
            and h.pattern is not None and h.pattern.match(payload)
        ]
        assert len(routed) == 1

    def test_the_decode_the_handler_runs_hands_back_the_level(self):
        assert session_picker.decode(effort_picker.encode("xhigh")) == (
            effort_picker.CB_EFFORT, "xhigh",
        )

    def test_no_other_pickers_pattern_captures_an_effort_payload(self):
        payload = effort_picker.encode("high")
        assert re.match(model_picker.CALLBACK_PATTERN, payload) is None
        assert re.match(session_picker.CALLBACK_PATTERN, payload) is None


class TestDiscord:
    @pytest.mark.asyncio
    async def test_a_typed_level_reaches_the_gateway_and_reports_the_set(self):
        asked = AsyncMock(return_value={"id": "s1", "effort": "high"})

        with patch.object(db, "gateway", MagicMock(server_command=asked)):
            msg = await db._handle_server_command("/effort high", 1, 2)

        assert asked.await_args.args[2] == "/effort high"
        assert msg == "Effort set to high"

    @pytest.mark.asyncio
    async def test_a_bare_effort_lists_the_levels_with_the_current_marked(self):
        with patch.object(db, "gateway", MagicMock(server_command=AsyncMock(return_value=LEVELS))):
            msg = await db._handle_server_command("/effort", 1, 2)

        assert "low, medium, **high** ← current, max" in msg

    @pytest.mark.asyncio
    async def test_a_bare_model_lists_the_models_rather_than_switching_to_none(self):
        answer = {"models": [{"name": "claude-opus-5", "provider": "anthropic"}],
                  "current": "claude-opus-5"}
        with patch.object(db, "gateway", MagicMock(server_command=AsyncMock(return_value=answer))):
            msg = await db._handle_server_command("/model", 1, 2)

        assert "claude-opus-5" in msg
        assert "None" not in msg
