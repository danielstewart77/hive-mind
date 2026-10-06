"""Renaming by reply: tap the command, type the name.

Requirements 2 through 7. Tapping a command in Telegram's menu *sends* it, so
a `/rename` that answers "Usage: /rename <name>" is a menu entry that cannot
be used from the menu. It asks instead, and the reply carries the name.

The recognition is held nowhere — no pending-prompt registry, no expiry — so a
prompt sent before a restart is answerable after one. That is requirement 6,
and it is the reason `name_from_reply` takes message text rather than an id.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_update(
    text: str = "",
    reply_to_text: str | None = None,
    user_id: int = 123,
    chat_id: int = 456,
):
    """A Telegram update, optionally replying to a message carrying ``reply_to_text``."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.effective_chat.type = "private"
    update.message.text = text
    update.message.reply_text = AsyncMock()
    if reply_to_text is None:
        update.message.reply_to_message = None
    else:
        update.message.reply_to_message = MagicMock()
        update.message.reply_to_message.text = reply_to_text
    return update


@pytest.fixture(autouse=True)
def _patch_config():
    with patch("hive_surfaces.telegram_bot.config") as mock_config:
        mock_config.telegram_allowed_users = {123}
        yield mock_config


@pytest.fixture()
def gateway():
    """A gateway that finds the chat's conversation and accepts a rename."""
    gw = AsyncMock()
    gw.find_active_session = AsyncMock(return_value="sess-1")
    gw.rename_session = AsyncMock(return_value=200)
    with patch("hive_surfaces.telegram_bot.gateway", gw):
        yield gw


class TestReplyRecognition:
    """The pure decision: is this message a name, or a turn?"""

    def test_reply_to_the_prompt_yields_the_name(self) -> None:
        """Requirement 3: replying to the prompt with a name renames."""
        from hive_surfaces import rename_prompt

        # Sourced from live state: the prompt the module actually sends is the
        # prompt a reply is recognised against. A hardcoded copy here would
        # pass while the two had drifted apart.
        assert rename_prompt.name_from_reply(
            rename_prompt.PROMPT_TEXT, "Dragoman"
        ) == "Dragoman"

    def test_reply_to_anything_else_is_not_a_name(self) -> None:
        """Requirement 5: an ordinary reply is an ordinary message."""
        from hive_surfaces import rename_prompt

        assert rename_prompt.name_from_reply(
            "Renamed to \"Dragoman\".", "what did that do?"
        ) is None

    def test_a_message_replying_to_nothing_is_not_a_name(self) -> None:
        """Requirement 5: the common case — a plain message is a turn."""
        from hive_surfaces import rename_prompt

        assert rename_prompt.name_from_reply(None, "Dragoman") is None

    def test_a_blank_reply_names_nothing(self) -> None:
        """Requirement 3: an empty reply must not reach the label store.

        The terminal deletes the label row when name and colour are both blank,
        so a whitespace reply that got through would erase the name rather than
        set one.
        """
        from hive_surfaces import rename_prompt

        assert rename_prompt.name_from_reply(rename_prompt.PROMPT_TEXT, "   ") is None

    def test_a_name_beginning_like_the_prompt_is_not_the_prompt(self) -> None:
        """Requirement 5: recognition is the whole message, not a prefix."""
        from hive_surfaces import rename_prompt

        near = rename_prompt.PROMPT_TEXT[:20]
        assert rename_prompt.name_from_reply(near, "Dragoman") is None


@pytest.mark.asyncio
class TestBareRenameAsks:
    async def test_bare_rename_sends_a_force_reply_prompt(self, gateway) -> None:
        """Requirement 2: `/rename` with no name asks for one."""
        from hive_surfaces.telegram_bot import cmd_rename
        from hive_surfaces import rename_prompt

        update = _make_update()
        context = MagicMock()
        context.args = []

        await cmd_rename(update, context)

        update.message.reply_text.assert_awaited_once()
        args, kwargs = update.message.reply_text.call_args
        assert args[0] == rename_prompt.PROMPT_TEXT
        assert kwargs.get("reply_markup") is not None
        gateway.rename_session.assert_not_awaited()

    async def test_bare_rename_with_no_conversation_refuses_without_asking(
        self, gateway
    ) -> None:
        """Requirement 7: don't ask for a name there is nothing to apply to."""
        from hive_surfaces.telegram_bot import cmd_rename
        from hive_surfaces import rename_prompt

        gateway.find_active_session = AsyncMock(return_value=None)
        update = _make_update()
        context = MagicMock()
        context.args = []

        await cmd_rename(update, context)

        args, kwargs = update.message.reply_text.call_args
        assert args[0] != rename_prompt.PROMPT_TEXT
        assert kwargs.get("reply_markup") is None
        gateway.rename_session.assert_not_awaited()

    async def test_rename_with_a_name_writes_it_directly(self, gateway) -> None:
        """Requirement 4: `/rename Dragoman` still renames in one go."""
        from hive_surfaces.telegram_bot import cmd_rename
        from hive_surfaces import rename_prompt

        update = _make_update()
        context = MagicMock()
        context.args = ["Dragoman"]

        await cmd_rename(update, context)

        gateway.rename_session.assert_awaited_once()
        session_id, body = gateway.rename_session.call_args[0]
        assert session_id == "sess-1"
        assert body["name"] == "Dragoman"
        # The name alone. The write is partial, so the colour set at the tile
        # survives by not being mentioned — sending it is how a rename used to
        # blank it whenever the read that supplied it failed.
        assert "color" not in body
        assert update.message.reply_text.call_args[0][0] != rename_prompt.PROMPT_TEXT


@pytest.mark.asyncio
class TestReplyRenamesTheConversation:
    async def test_reply_to_prompt_writes_the_name(self, gateway) -> None:
        """Requirement 3: the reply renames the conversation this chat is in."""
        from hive_surfaces.telegram_bot import handle_text
        from hive_surfaces import rename_prompt

        update = _make_update("Dragoman", reply_to_text=rename_prompt.PROMPT_TEXT)
        context = MagicMock()

        with patch("hive_surfaces.telegram_bot._stream_to_message", AsyncMock()) as stream:
            await handle_text(update, context)

        gateway.rename_session.assert_awaited_once()
        assert gateway.rename_session.call_args[0][1]["name"] == "Dragoman"
        # Requirement 3: the name is a name, not a question for the mind.
        stream.assert_not_awaited()

    async def test_ordinary_message_reaches_the_harness(self, gateway) -> None:
        """Requirement 5: dismissing the prompt and typing works as it always did."""
        from hive_surfaces.telegram_bot import handle_text

        update = _make_update("what is the status of the deploy?")
        context = MagicMock()

        with patch("hive_surfaces.telegram_bot._stream_to_message", AsyncMock(return_value=["ok"])) as stream:
            await handle_text(update, context)

        stream.assert_awaited()
        gateway.rename_session.assert_not_awaited()


class TestOnlyTheBotsOwnPromptCounts:
    def test_the_same_words_from_anyone_else_are_not_the_prompt(self) -> None:
        """Requirement 5: a name is never taken from a message the bot didn't send.

        The operator can type the prompt's exact words themselves, and a mind
        asked what `/rename` does may quote them verbatim. Replying to either
        would otherwise write a label.
        """
        from hive_surfaces import rename_prompt

        assert rename_prompt.name_from_reply(
            rename_prompt.PROMPT_TEXT, "Dragoman", False
        ) is None
        assert rename_prompt.name_from_reply(
            rename_prompt.PROMPT_TEXT, "Dragoman", True
        ) == "Dragoman"

    def test_a_reply_too_long_to_be_a_name_is_not_taken_as_one(self) -> None:
        """Requirement 3: a changed mind must not become the conversation's name.

        Truncating to forty characters named the conversation with the head of
        whatever was typed and swallowed the rest, so replying "actually never
        mind, what is the status of the deploy?" both renamed the conversation
        to a sentence fragment and lost the question.
        """
        from hive_surfaces import rename_prompt

        sentence = "actually never mind, what is the status of the deploy on comms?"
        assert len(sentence) > rename_prompt.MAX_NAME_CHARS
        assert rename_prompt.name_from_reply(rename_prompt.PROMPT_TEXT, sentence) is None
        # A name at the boundary is still a name.
        at_limit = "x" * rename_prompt.MAX_NAME_CHARS
        assert rename_prompt.name_from_reply(
            rename_prompt.PROMPT_TEXT, at_limit
        ) == at_limit


@pytest.mark.asyncio
class TestTheReplyIsFoundOnEverySurface:
    async def test_a_group_reply_carrying_no_mention_still_renames(
        self, gateway
    ) -> None:
        """Requirement 3: ForceReply aims the composer, so no mention is typed.

        The group @mention gate used to run first and drop the reply outright —
        no rename, no turn, no answer, nothing in the log. The bot looked dead.
        """
        from hive_surfaces.telegram_bot import handle_text
        from hive_surfaces import rename_prompt

        update = _make_update("Dragoman", reply_to_text=rename_prompt.PROMPT_TEXT)
        update.effective_chat.type = "group"
        context = MagicMock()
        context.bot.username = "skippybot"
        context.bot.send_message = AsyncMock()

        await handle_text(update, context)

        gateway.rename_session.assert_awaited_once()
        assert gateway.rename_session.call_args[0][1]["name"] == "Dragoman"

    async def test_a_spoken_reply_renames(self, gateway) -> None:
        """Requirement 3: the prompt opens a reply box on a voice-first surface.

        Telegram is the primary surface here and runs with voice on, so the
        obvious way to answer an open reply box is to speak the name. Without
        this the transcript went to the harness and the mind answered
        "Dragoman?" while nothing was renamed.
        """
        from hive_surfaces.telegram_bot import handle_voice
        from hive_surfaces import rename_prompt

        update = _make_update("", reply_to_text=rename_prompt.PROMPT_TEXT)
        update.message.voice = MagicMock()
        context = MagicMock()
        context.bot.send_message = AsyncMock()

        with patch("hive_surfaces.telegram_bot._stt", AsyncMock(return_value="Dragoman")), \
                patch("hive_surfaces.telegram_bot._stream_to_message", AsyncMock()) as stream:
            update.message.voice.get_file = AsyncMock()
            update.message.voice.get_file.return_value.download_as_bytearray = AsyncMock(
                return_value=bytearray(b"ogg")
            )
            await handle_voice(update, context)

        gateway.rename_session.assert_awaited_once()
        assert gateway.rename_session.call_args[0][1]["name"] == "Dragoman"
        stream.assert_not_awaited()


@pytest.mark.asyncio
class TestRenameRefusesLoudly:
    async def test_an_unreachable_gateway_refuses_instead_of_vanishing(
        self, gateway
    ) -> None:
        """Requirements 2 and 7: no path through /rename produces silence.

        `find_active_session` raises aiohttp errors, not telegram ones, so the
        exception escaped the handler entirely — the operator tapped the menu
        entry and got neither the prompt nor a refusal.
        """
        from hive_surfaces.telegram_bot import cmd_rename
        from hive_surfaces import rename_prompt

        gateway.find_active_session = AsyncMock(side_effect=OSError("connection refused"))
        update = _make_update()
        context = MagicMock()
        context.args = []

        await cmd_rename(update, context)

        update.message.reply_text.assert_awaited_once()
        said = update.message.reply_text.call_args[0][0]
        assert said.strip()
        assert said != rename_prompt.PROMPT_TEXT


@pytest.mark.asyncio
class TestTheRenamePromptHasATarget:
    async def test_the_prompt_quotes_the_command_that_asked_for_it(
        self, gateway
    ) -> None:
        """Requirement 2: `selective` needs something to select.

        The Bot API targets a force-reply at "users @mentioned in the text" or,
        if the bot's message is a reply, "the sender of the original". The
        prompt mentions nobody, and PTB does not quote in a private chat unless
        told to — so without this the reply box opens for nobody and the
        keyboard does not come up aimed at the prompt.
        """
        from hive_surfaces.telegram_bot import cmd_rename

        update = _make_update()
        context = MagicMock()
        context.args = []

        await cmd_rename(update, context)

        assert update.message.reply_text.call_args.kwargs.get("do_quote") is True


@pytest.mark.asyncio
class TestAnEditedMessageIsNotARename:
    async def test_an_update_carrying_no_message_is_ignored(self) -> None:
        """Editing a sent message re-fires these handlers with no `message`.

        The helper guards for it, but a caller dereferencing `update.message`
        to build the argument defeats that guard — the argument is evaluated
        first, so the AttributeError happens before the guard is reached.
        """
        from hive_surfaces.telegram_bot import _handled_as_rename_reply

        update = MagicMock()
        update.message = None

        assert await _handled_as_rename_reply(update, MagicMock(), "Dragoman") is False
