"""`/model` offers the models as buttons, and never claims a switch it didn't make.

Two defects sat here. The names are deployment names (`claude-sonnet-5-5`,
`gpt-5.6-terra`), so `/model <name>` asked the operator to transcribe one on a
phone — and tapping the command in Telegram's menu *sends* it bare, which is
the easiest way in the world to reach the no-argument path. That path then
reported "Switched to None": the gateway answers a bare `/model` with
``{"models": [...]}``, the bot tested the answer for being a *list*, a dict is
not one, so the listing fell through to the switch-report branch and read a
`model` field the answer does not carry.
"""

import re

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import hive_surfaces.telegram_bot as tb
from hive_surfaces import model_picker, session_picker
from hive_surfaces.config import SurfaceConfig, config, configure

GATEWAY_ANSWER = {
    "models": [
        {"name": "claude-opus-5-5", "provider_label": "Azure"},
        {"name": "claude-sonnet-5-5", "provider_label": "Azure"},
        {"name": "qwen3-coder", "provider": "ollama"},
    ]
}


@pytest.fixture(autouse=True)
def _restore_config():
    before = config._installed()
    yield
    configure(before)


def _allow(**fields):
    configure(SurfaceConfig(telegram_allowed_users=[123], **fields))


def _authorized_update(args=None):
    update = MagicMock()
    update.effective_user.id = 123
    update.effective_chat.id = 456
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = args or []
    context.bot.send_message = AsyncMock()
    return update, context


def _payloads(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def _labels(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


class TestTheKeyboard:
    @pytest.mark.asyncio
    async def test_a_bare_model_draws_one_button_per_model_in_the_order_given(self) -> None:
        """The mind asked the proxy; re-ordering here would make the keyboard
        disagree with `/models`, which prints the same answer."""
        _allow()
        update, context = _authorized_update()

        with patch.object(tb, "gateway",
                          MagicMock(server_command=AsyncMock(return_value=GATEWAY_ANSWER))):
            await tb.cmd_model(update, context)

        markup = context.bot.send_message.await_args.kwargs["reply_markup"]
        assert _payloads(markup) == [
            model_picker.encode("claude-opus-5-5"),
            model_picker.encode("claude-sonnet-5-5"),
            model_picker.encode("qwen3-coder"),
        ]

    @pytest.mark.asyncio
    async def test_a_bare_model_asks_the_gateway_for_the_listing_not_a_switch(self) -> None:
        """A bare `/model` must reach the gateway as a bare `/model`. Sending
        it with a trailing anything is how a listing becomes a switch."""
        _allow()
        update, context = _authorized_update()

        asked = AsyncMock(return_value=GATEWAY_ANSWER)
        with patch.object(tb, "gateway", MagicMock(server_command=asked)):
            await tb.cmd_model(update, context)

        assert asked.await_args.args[2] == "/model"

    @pytest.mark.asyncio
    async def test_the_model_this_conversation_is_on_is_the_one_marked(self) -> None:
        """Not the mind's configured default. That is a different fact, and on
        an edge install it is a pre-proxy alias ("opus") matching no deployment
        name in the catalog ("claude-opus-5") — so marking it marked nothing,
        and this test used to pass only by injecting a deployment name where
        production supplies an alias."""
        _allow(default_model="opus")
        update, context = _authorized_update()
        answer = dict(GATEWAY_ANSWER, current="claude-sonnet-5-5")

        with patch.object(tb, "gateway",
                          MagicMock(server_command=AsyncMock(return_value=answer))):
            await tb.cmd_model(update, context)

        marked = [
            label for label in _labels(context.bot.send_message.await_args.kwargs["reply_markup"])
            if "✓" in label
        ]
        assert marked == ["claude-sonnet-5-5 · Azure ✓"]

    @pytest.mark.asyncio
    async def test_a_gateway_naming_no_current_model_marks_nothing(self) -> None:
        """An older gateway answers the listing alone. A tick invented from the
        mind's default would mark a button the conversation is not on."""
        _allow(default_model="claude-opus-5-5")
        update, context = _authorized_update()

        with patch.object(tb, "gateway",
                          MagicMock(server_command=AsyncMock(return_value=GATEWAY_ANSWER))):
            await tb.cmd_model(update, context)

        labels = _labels(context.bot.send_message.await_args.kwargs["reply_markup"])
        assert not any("✓" in label for label in labels)

    @pytest.mark.asyncio
    async def test_a_truncated_keyboard_says_how_many_it_is_showing(self) -> None:
        """The cap is two Ollama tags away on this hive, and the proxy's order
        carries no property that makes the survivors the right ones to keep."""
        _allow()
        update, context = _authorized_update()
        many = {"models": [
            {"name": f"model-{i}"} for i in range(model_picker.MAX_PICKER_ROWS + 5)
        ]}

        with patch.object(tb, "gateway",
                          MagicMock(server_command=AsyncMock(return_value=many))):
            await tb.cmd_model(update, context)

        header = context.bot.send_message.await_args.kwargs["text"]
        assert str(model_picker.MAX_PICKER_ROWS) in header
        assert str(model_picker.MAX_PICKER_ROWS + 5) in header

    @pytest.mark.asyncio
    async def test_a_send_that_fails_every_attempt_says_so_rather_than_nothing(self) -> None:
        """A bare send fails into the error handler, which logs a transient
        network error at INFO and returns — so the operator taps the command
        and sees absolutely nothing."""
        _allow()
        update, context = _authorized_update()
        context.bot.send_message = AsyncMock(side_effect=RuntimeError("Bad Gateway"))

        with patch.object(tb, "gateway",
                          MagicMock(server_command=AsyncMock(return_value=GATEWAY_ANSWER))), \
                patch.object(tb, "_deliver", new=AsyncMock()) as delivered, \
                patch.object(tb, "_DELIVER_BACKOFF_S", 0):
            await tb.cmd_model(update, context)

        assert context.bot.send_message.await_count == tb._DELIVER_ATTEMPTS
        assert "model list" in delivered.await_args.args[2].lower()

    @pytest.mark.asyncio
    async def test_a_mind_offering_nothing_gets_a_sentence_and_no_keyboard(self) -> None:
        """An empty keyboard renders as a message with a blank attachment,
        which reads as a picker that failed rather than a mind with nothing."""
        _allow()
        update, context = _authorized_update()

        with patch.object(tb, "gateway",
                          MagicMock(server_command=AsyncMock(return_value={"models": []}))), \
                patch.object(tb, "_deliver", new=AsyncMock()) as delivered:
            await tb.cmd_model(update, context)

        assert context.bot.send_message.await_count == 0
        assert "no models offered" in delivered.await_args.args[2].lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["error", "detail"])
    async def test_a_gateway_refusal_is_reported_rather_than_read_as_no_models(self, key) -> None:
        """"No active session" and "the proxy is down" have different remedies.
        FastAPI's own rejections arrive as `detail`, the gateway's as `error`."""
        _allow()
        update, context = _authorized_update()

        with patch.object(tb, "gateway", MagicMock(server_command=AsyncMock(
                    return_value={key: "No active session. Use /new first."}))), \
                patch.object(tb, "_deliver", new=AsyncMock()) as delivered:
            await tb.cmd_model(update, context)

        assert "No active session" in delivered.await_args.args[2]
        assert context.bot.send_message.await_count == 0

    @pytest.mark.asyncio
    async def test_a_named_model_still_switches_without_drawing_a_keyboard(self) -> None:
        _allow()
        update, context = _authorized_update(args=["claude-sonnet-5-5"])

        with patch.object(tb, "_handle_server_command",
                          new=AsyncMock(return_value="Switched to claude-sonnet-5-5")) as sent:
            await tb.cmd_model(update, context)

        assert sent.await_args.args[0] == "/model claude-sonnet-5-5"
        assert context.bot.send_message.await_count == 0

    def test_a_name_too_long_for_a_callback_is_dropped_rather_than_breaking_the_board(self) -> None:
        """Telegram rejects the whole sendMessage over one oversized payload,
        so an unfittable name costs its own button and nothing else."""
        rows = {"models": [
            {"name": "a" * (model_picker.CALLBACK_DATA_LIMIT + 1)},
            {"name": "claude-opus-5-5"},
        ]}

        assert _payloads(model_picker.build_model_keyboard(rows)) == [
            model_picker.encode("claude-opus-5-5")
        ]


    def test_a_name_whose_payload_exactly_fills_a_callback_is_kept(self) -> None:
        """The limit is Telegram's own, inclusive: 64 bytes is legal."""
        prefix = len(model_picker.encode(""))
        name = "a" * (model_picker.CALLBACK_DATA_LIMIT - prefix)

        assert _payloads(model_picker.build_model_keyboard({"models": [{"name": name}]})) == [
            model_picker.encode(name)
        ]


def _model_tap(name: str, message_id: int = 99):
    query = MagicMock()
    query.data = model_picker.encode(name)
    query.answer = AsyncMock()
    query.message.message_id = message_id
    query.message.text = "Pick a model:"
    query.edit_message_reply_markup = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    update.effective_user.id = 123
    update.effective_chat.id = 456
    context = MagicMock()
    context.bot = MagicMock()
    return update, context


class TestTappingAButton:
    @pytest.mark.asyncio
    async def test_a_second_tap_on_one_model_picker_switches_nothing(
        self, tmp_path, monkeypatch,
    ) -> None:
        """A model picker left in scrollback must not switch a conversation
        weeks later — the claim covers model taps, not only conversation taps."""
        from hive_surfaces import bot_utils

        monkeypatch.setenv("PICKER_STATE_PATH", str(tmp_path / "spent_pickers.json"))
        bot_utils._reset_pickers()
        _allow()
        try:
            with patch.object(tb, "_handle_server_command",
                              new=AsyncMock(return_value="Switched to qwen3-coder")) as sent, \
                    patch.object(tb, "_deliver", new=AsyncMock()):
                for _ in range(2):
                    await tb.on_session_button(*_model_tap("qwen3-coder"))
        finally:
            bot_utils._reset_pickers()

        assert sent.await_count == 1

    def test_the_running_application_routes_a_model_tap_to_a_handler(self) -> None:
        """Every other test calls the handler directly, so a dropped
        registration would leave every model button inert behind a green suite."""
        from telegram.ext import CallbackQueryHandler

        with patch.object(tb, "_on_startup"), patch.object(tb, "_on_shutdown"):
            app = tb._build_application("123456:fake-token-for-tests")
        payload = model_picker.encode("qwen3-coder")

        routed = [
            h for group in app.handlers.values() for h in group
            if isinstance(h, CallbackQueryHandler)
            and h.pattern is not None and h.pattern.match(payload)
        ]
        assert len(routed) == 1


    @pytest.mark.asyncio
    async def test_a_tapped_model_is_the_model_switched_to(self) -> None:
        with patch.object(tb, "_handle_server_command",
                          new=AsyncMock(return_value="Switched to qwen3-coder")) as sent:
            worked, msg = await tb._run_session_button(
                model_picker.CB_PICK, "qwen3-coder", 123, 456,
            )

        assert sent.await_args.args[0] == "/model qwen3-coder"
        assert worked is True
        assert "qwen3-coder" in msg

    @pytest.mark.asyncio
    async def test_a_refused_switch_is_reported_as_a_failure(self) -> None:
        """A model withdrawn since the keyboard was drawn, or a turn still
        streaming — the tap must not be ticked as having worked."""
        with patch.object(tb, "_handle_server_command",
                          new=AsyncMock(return_value="Error: This conversation is mid-answer.")):
            worked, msg = await tb._run_session_button(
                model_picker.CB_PICK, "qwen3-coder", 123, 456,
            )

        assert worked is False
        assert "mid-answer" in msg

    def test_the_decode_the_handler_runs_keeps_a_colon_bearing_name_whole(self) -> None:
        """Ollama deployment names carry a tag separator, and the callback
        payload uses the same character — so the decode that actually runs
        (`session_picker.decode`, which both pickers share) has to hand back
        the whole name, not the part before the second colon."""
        name = "qwen3:30b-a3b-instruct-2507-q4_K_M"

        action, target = session_picker.decode(model_picker.encode(name))

        assert action == model_picker.CB_PICK
        assert target == name

    def test_neither_pickers_pattern_captures_the_others_payloads(self) -> None:
        """Both are registered on one handler; a session payload resolving as
        a model would switch a model named after a conversation id."""
        assert re.match(model_picker.CALLBACK_PATTERN, "sw:4f1c-abc") is None
        assert re.match(model_picker.CALLBACK_PATTERN, "new") is None
        assert re.match(session_picker.CALLBACK_PATTERN, model_picker.encode("opus")) is None


class TestTheSwitchReport:
    def test_a_listing_names_the_models_it_was_given_and_claims_no_switch(self) -> None:
        """The defect verbatim: a dict of models rendered as "Switched to None".

        The names are asserted, not only the absence of "switched" — a
        constant string would satisfy an absence and tell the operator
        nothing about what this mind can run.
        """
        reported = tb.format_model_result(GATEWAY_ANSWER)

        for row in GATEWAY_ANSWER["models"]:
            assert row["name"] in reported
        assert "Azure" in reported and "ollama" in reported
        assert "switched" not in reported.lower()
        assert "none" not in reported.lower()

    def test_an_answer_naming_no_model_claims_no_switch(self) -> None:
        reported = tb.format_model_result({})

        assert "no model change" in reported.lower()

    def test_an_answer_naming_a_model_reports_that_model(self) -> None:
        reported = tb.format_model_result({"model": "claude-opus-5-5"})

        assert reported == "Switched to claude-opus-5-5"


class TestARefusedTap:
    @pytest.mark.asyncio
    async def test_a_refused_model_tap_redraws_models_not_conversations(self) -> None:
        """A picker is spent by its one tap and never given back, so a failed
        tap is handed a fresh list. Handing back the *conversation* list gave
        the operator a single-use keyboard they never asked for, whose rows
        retarget ownership and end a browser terminal."""
        _allow()
        query = MagicMock()
        query.data = model_picker.encode("qwen3-coder")
        query.answer = AsyncMock()
        query.message.message_id = 99
        query.message.text = "Pick a model:"
        query.edit_message_reply_markup = AsyncMock()
        query.edit_message_text = AsyncMock()
        update = MagicMock()
        update.callback_query = query
        update.effective_user.id = 123
        update.effective_chat.id = 456
        context = MagicMock()
        context.bot = MagicMock()

        with patch.object(tb, "_handle_server_command",
                          new=AsyncMock(return_value="Error: mid-answer")), \
                patch.object(tb, "claim_picker", return_value=True), \
                patch.object(tb, "_deliver", new=AsyncMock()), \
                patch.object(tb, "_send_model_picker", new=AsyncMock()) as models, \
                patch.object(tb, "_send_session_picker", new=AsyncMock()) as sessions:
            await tb.on_session_button(update, context)

        assert models.await_count == 1
        assert sessions.await_count == 0
