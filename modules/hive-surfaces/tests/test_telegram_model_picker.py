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

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import hive_surfaces.telegram_bot as tb
from hive_surfaces import model_picker
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
    async def test_the_minds_own_default_is_marked_on_its_button(self) -> None:
        _allow(default_model="claude-sonnet-5-5")
        update, context = _authorized_update()

        with patch.object(tb, "gateway",
                          MagicMock(server_command=AsyncMock(return_value=GATEWAY_ANSWER))):
            await tb.cmd_model(update, context)

        marked = [
            label for label in _labels(context.bot.send_message.await_args.kwargs["reply_markup"])
            if "✓" in label
        ]
        assert marked == ["claude-sonnet-5-5 · Azure ✓"]

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
    async def test_a_gateway_refusal_is_reported_rather_than_read_as_no_models(self) -> None:
        """"No active session" and "the proxy is down" have different remedies."""
        _allow()
        update, context = _authorized_update()

        with patch.object(tb, "gateway", MagicMock(server_command=AsyncMock(
                    return_value={"error": "No active session. Use /new first."}))), \
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


class TestTappingAButton:
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

    def test_a_model_payload_decodes_to_its_own_name_and_nothing_elses(self) -> None:
        assert model_picker.decode(model_picker.encode("gpt-5.6-terra")) == "gpt-5.6-terra"
        # A session-picker payload must never resolve to a model: both travel
        # through one callback handler.
        assert model_picker.decode("sw:4f1c-not-a-model") == ""
        assert model_picker.decode("new") == ""


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
