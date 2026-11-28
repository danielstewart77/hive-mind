"""`/harness` moves a conversation between harnesses, from buttons or typed.

Tests 28 to 30 of the pluggable-harness feature. The gateway answers a bare
`/harness` with what the mind offers, `/harness <name>` with that harness's
models, and `/harness <name> <model>` with the switched session — so these
tests stub the gateway transport with those answers and assert on what the
operator would see and on what the bot sends back.
"""

import re
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import discord

import pytest

import hive_surfaces.discord_bot as db
import hive_surfaces.telegram_bot as tb
from hive_surfaces import effort_picker, harness_picker, model_picker, session_picker
from hive_surfaces.config import SurfaceConfig, config, configure

HARNESSES = {
    "harnesses": [
        {"name": "claude", "available": True, "reason": ""},
        {"name": "codex", "available": True, "reason": ""},
        {"name": "dsh", "available": False, "reason": "not installed on this host"},
    ],
    "current": "claude",
}
CODEX_MODELS = {
    "harness": "codex",
    "models": [{"name": "gpt-5.6-terra", "provider": "azure"},
               {"name": "gpt-5.6-terra-mini", "provider": "azure"}],
    "current": "claude-opus-5",
}
SWITCHED = {"id": "s1", "harness": "codex", "model": "gpt-5.6-terra", "name": "dragoman"}


@pytest.fixture(autouse=True)
def _restore_config():
    before = config._installed()
    configure(SurfaceConfig(telegram_allowed_users=[123]))
    yield
    configure(before)


@pytest.fixture(autouse=True)
def _clean_pickers(tmp_path, monkeypatch):
    monkeypatch.setenv("PICKER_STATE_PATH", str(tmp_path / "spent_pickers.json"))
    from hive_surfaces import bot_utils

    bot_utils._reset_pickers()
    yield
    bot_utils._reset_pickers()


def _gateway(*answers):
    """The transport, answering each `server_command` by its command text."""
    table = dict(answers)

    async def server_command(user_id, chat_id, content):
        return table.get(content, {"error": f"unexpected {content!r}"})

    asked = AsyncMock(side_effect=server_command)
    return MagicMock(server_command=asked, find_active_session=AsyncMock(return_value="s1")), asked


def _update(args=None):
    update = MagicMock()
    update.effective_user.id = 123
    update.effective_chat.id = 456
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = args or []
    context.bot.send_message = AsyncMock()
    return update, context


def _tap(payload: str, message_id: int = 99):
    query = MagicMock()
    query.data = payload
    query.answer = AsyncMock()
    query.message.message_id = message_id
    query.message.text = "Pick a harness:"
    query.edit_message_reply_markup = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    update.effective_user.id = 123
    update.effective_chat.id = 456
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    return update, context


def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def _handler_for(app, payload):
    from telegram.ext import CallbackQueryHandler

    return [
        h for group in app.handlers.values() for h in group
        if isinstance(h, CallbackQueryHandler)
        and h.pattern is not None and h.pattern.match(payload)
    ]


class TestTheTelegramPicker:
    """Test 28: a button per offered harness, then its models, then one switch."""

    @pytest.mark.asyncio
    async def test_a_bare_harness_draws_a_button_per_available_harness_with_the_current_ticked(self):
        update, context = _update()
        gateway, asked = _gateway(("/harness", HARNESSES))

        with patch.object(tb, "gateway", gateway):
            await tb.cmd_harness(update, context)

        assert asked.await_args.args[2] == "/harness"
        sent = context.bot.send_message.await_args.kwargs
        buttons = _buttons(sent["reply_markup"])
        assert [b.callback_data for b in buttons] == [
            harness_picker.encode_harness("claude"), harness_picker.encode_harness("codex"),
        ]
        assert [b.text for b in buttons if "✓" in b.text] == ["claude ✓"]
        # The unavailable one is named with its reason, and is not a button.
        assert "dsh" in sent["text"] and "not installed on this host" in sent["text"]

    @pytest.mark.asyncio
    async def test_tapping_a_harness_turns_the_picker_into_that_harnesss_models(self):
        update, context = _tap(harness_picker.encode_harness("codex"))
        gateway, asked = _gateway(("/harness codex", CODEX_MODELS))

        with patch.object(tb, "gateway", gateway):
            await tb.on_harness_button(update, context)

        assert [c.args[2] for c in asked.await_args_list] == ["/harness codex"]
        edit = update.callback_query.edit_message_text.await_args
        assert "codex" in edit.args[0]
        assert [b.callback_data for b in _buttons(edit.kwargs["reply_markup"])] == [
            harness_picker.encode_model("codex", "gpt-5.6-terra"),
            harness_picker.encode_model("codex", "gpt-5.6-terra-mini"),
        ]

    @pytest.mark.asyncio
    async def test_tapping_a_model_sends_exactly_one_switch_naming_harness_and_model(self):
        update, context = _tap(harness_picker.encode_model("codex", "gpt-5.6-terra"))
        gateway, asked = _gateway(("/harness codex gpt-5.6-terra", SWITCHED))

        with patch.object(tb, "gateway", gateway), patch.object(tb, "_deliver", new=AsyncMock()) as said:
            await tb.on_session_button(update, context)

        assert [c.args[2] for c in asked.await_args_list] == ["/harness codex gpt-5.6-terra"]
        assert "codex" in said.await_args.args[2] and "gpt-5.6-terra" in said.await_args.args[2]

    @pytest.mark.asyncio
    async def test_a_model_name_carrying_a_colon_reaches_the_switch_whole(self):
        """An Ollama tag is `name:size`; the harness is split off, not the tag."""
        update, context = _tap(harness_picker.encode_model("dsh", "qwen3:32b"))
        gateway, asked = _gateway(("/harness dsh qwen3:32b", dict(SWITCHED, harness="dsh", model="qwen3:32b")))

        with patch.object(tb, "gateway", gateway), patch.object(tb, "_deliver", new=AsyncMock()):
            await tb.on_session_button(update, context)

        assert [c.args[2] for c in asked.await_args_list] == ["/harness dsh qwen3:32b"]

    @pytest.mark.asyncio
    async def test_a_refused_switch_is_a_failure_and_redraws_the_harness_list(self):
        update, context = _tap(harness_picker.encode_model("codex", "gpt-5.6-terra"))
        gateway, asked = _gateway(
            ("/harness codex gpt-5.6-terra", {"error": "This conversation is mid-answer."}),
            ("/harness", HARNESSES),
        )

        with patch.object(tb, "gateway", gateway), patch.object(tb, "_deliver", new=AsyncMock()) as said:
            await tb.on_session_button(update, context)

        assert "mid-answer" in said.await_args_list[0].args[2]
        assert [c.args[2] for c in asked.await_args_list] == [
            "/harness codex gpt-5.6-terra", "/harness",
        ]

    def test_a_model_whose_payload_would_exceed_telegrams_limit_gets_no_button(self):
        long_name = "m" * 70
        markup = harness_picker.build_model_keyboard(
            "codex", {"models": [{"name": long_name}, {"name": "gpt-5.6-terra"}]},
        )

        payloads = [b.callback_data for b in _buttons(markup)]
        assert payloads == [harness_picker.encode_model("codex", "gpt-5.6-terra")]
        assert all(len(p.encode("utf-8")) <= 64 for p in payloads)

    @pytest.mark.parametrize("payload", [
        harness_picker.encode_harness("codex"),
        harness_picker.encode_model("codex", "gpt-5.6-terra"),
    ])
    def test_the_running_application_routes_each_harness_tap_to_exactly_one_handler(self, payload):
        with patch.object(tb, "_on_startup"), patch.object(tb, "_on_shutdown"):
            app = tb._build_application("123456:fake-token-for-tests")

        assert len(_handler_for(app, payload)) == 1

    def test_the_harness_patterns_capture_no_model_or_effort_payload(self):
        for payload in (model_picker.encode("gpt-5.6-terra"), effort_picker.encode("high"),
                        session_picker.encode(session_picker.CB_SWITCH, "s1")):
            assert re.match(harness_picker.HARNESS_PATTERN, payload) is None
            assert re.match(harness_picker.MODEL_PATTERN, payload) is None

    def test_harness_is_on_the_command_menu(self):
        assert "harness" in [name for name, _description in tb.valid_menu_entries()]


    @pytest.mark.asyncio
    async def test_a_harness_tap_then_a_model_tap_on_the_same_picker_switches(self):
        """The harness tap navigates; it must leave the picker for the model tap."""
        gateway, asked = _gateway(
            ("/harness codex", CODEX_MODELS), ("/harness codex gpt-5.6-terra", SWITCHED),
        )

        with patch.object(tb, "gateway", gateway), patch.object(tb, "_deliver", new=AsyncMock()):
            await tb.on_harness_button(*_tap(harness_picker.encode_harness("codex"), message_id=99))
            await tb.on_session_button(
                *_tap(harness_picker.encode_model("codex", "gpt-5.6-terra"), message_id=99))

        assert [c.args[2] for c in asked.await_args_list] == [
            "/harness codex", "/harness codex gpt-5.6-terra",
        ]


class TestASpentHarnessPicker:
    """Test 29: a second tap on a spent harness picker sends nothing."""

    @pytest.mark.asyncio
    async def test_a_second_model_tap_on_one_picker_switches_nothing(self):
        gateway, asked = _gateway(("/harness codex gpt-5.6-terra", SWITCHED))

        with patch.object(tb, "gateway", gateway), patch.object(tb, "_deliver", new=AsyncMock()):
            for _ in range(2):
                await tb.on_session_button(*_tap(harness_picker.encode_model("codex", "gpt-5.6-terra")))

        assert [c.args[2] for c in asked.await_args_list] == ["/harness codex gpt-5.6-terra"]

    @pytest.mark.asyncio
    async def test_a_harness_tap_on_a_spent_picker_asks_nothing_and_draws_nothing(self):
        gateway, asked = _gateway(
            ("/harness codex gpt-5.6-terra", SWITCHED), ("/harness codex", CODEX_MODELS),
        )

        with patch.object(tb, "gateway", gateway), patch.object(tb, "_deliver", new=AsyncMock()):
            await tb.on_session_button(*_tap(harness_picker.encode_model("codex", "gpt-5.6-terra")))
            stale, context = _tap(harness_picker.encode_harness("codex"))
            await tb.on_harness_button(stale, context)

        assert [c.args[2] for c in asked.await_args_list] == ["/harness codex gpt-5.6-terra"]
        assert stale.callback_query.edit_message_text.await_count == 0


class TestDiscord:
    """Test 30: Discord has a /harness slash command that lists and switches."""

    def test_harness_is_a_slash_command_taking_an_optional_name_and_model(self):
        command = db.bot.tree.get_command("harness")

        assert command is not None
        params = {p.name: p.required for p in command.parameters}
        assert params == {"name": False, "model": False}

    @pytest.mark.asyncio
    async def test_a_bare_harness_lists_the_harnesses_with_current_and_unavailable_reason(self):
        gateway, asked = _gateway(("/harness", HARNESSES))

        with patch.object(db, "gateway", gateway):
            msg = await db._handle_server_command("/harness", 1, 2)

        assert asked.await_args.args[2] == "/harness"
        assert "claude" in msg and "current" in msg
        assert "dsh" in msg and "not installed on this host" in msg

    @pytest.mark.asyncio
    async def test_a_named_harness_lists_its_models(self):
        gateway, _ = _gateway(("/harness codex", CODEX_MODELS))

        with patch.object(db, "gateway", gateway):
            msg = await db._handle_server_command("/harness codex", 1, 2)

        assert "gpt-5.6-terra" in msg and "gpt-5.6-terra-mini" in msg

    @pytest.mark.asyncio
    async def test_a_harness_and_model_switch_and_report_both(self):
        gateway, asked = _gateway(("/harness codex gpt-5.6-terra", SWITCHED))

        with patch.object(db, "gateway", gateway):
            msg = await db._handle_server_command("/harness codex gpt-5.6-terra", 1, 2)

        assert asked.await_args.args[2] == "/harness codex gpt-5.6-terra"
        assert "codex" in msg and "gpt-5.6-terra" in msg

    @pytest.mark.asyncio
    async def test_the_registered_slash_command_sends_the_harness_and_model(self):
        interaction = MagicMock()
        interaction.user.id = 7
        interaction.channel_id = 2
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        gateway, asked = _gateway(("/harness codex gpt-5.6-terra", SWITCHED))
        configure(SurfaceConfig(discord_allowed_users=[7]))

        with patch.object(db, "gateway", gateway):
            await db.bot.tree.get_command("harness").callback(
                interaction, name="codex", model="gpt-5.6-terra")

        assert [c.args[2] for c in asked.await_args_list] == ["/harness codex gpt-5.6-terra"]
        assert "Switched to codex" in interaction.followup.send.await_args.args[0]

    @pytest.mark.asyncio
    async def test_a_typed_harness_message_is_a_command_not_a_prompt(self):
        message = MagicMock()
        message.author.id = 7
        message.content = "/harness codex gpt-5.6-terra"
        message.channel = MagicMock(spec=discord.DMChannel)
        message.channel.id = 2
        message.channel.typing.return_value.__aenter__ = AsyncMock()
        message.channel.typing.return_value.__aexit__ = AsyncMock(return_value=False)
        message.mentions = []
        message.reply = AsyncMock()
        gateway, asked = _gateway(("/harness codex gpt-5.6-terra", SWITCHED))
        configure(SurfaceConfig(discord_allowed_users=[7]))

        with patch.object(db, "gateway", gateway), \
                patch.object(type(db.bot), "user", new_callable=PropertyMock,
                             return_value=MagicMock(id=1)):
            await db.on_message(message)

        assert [c.args[2] for c in asked.await_args_list] == ["/harness codex gpt-5.6-terra"]
