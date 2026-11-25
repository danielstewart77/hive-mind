"""The indicator says "the mind is working", from arrival to reply.

It used to be held inside the streaming helper, which is not where the waiting
happens: a voice note is downloaded and transcribed before a single token
exists, a photo is fetched at full resolution, and every slash command is a
gateway round trip of its own. All of those showed nothing at all, and the
user reading an idle chat has no way to tell a long turn from a dropped one.

So it is held around every registered handler. What these guard is that it is
held there — not that one helper happens to hold it — because the regression
is a handler registered bare, which no test of the helper can see.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.constants import ChatAction

import hive_surfaces.telegram_bot as tb


def _fake_update(chat_id: int = 456):
    """An update with a bot whose actions can be counted."""
    bot = MagicMock()
    bot.send_chat_action = AsyncMock()
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = 123
    update.get_bot = MagicMock(return_value=bot)
    update.message.reply_text = AsyncMock()
    return update, bot


class TestHoldingTheIndicatorAroundAHandler:
    @pytest.mark.asyncio
    async def test_the_indicator_is_showing_before_the_handler_body_runs(self) -> None:
        """A handler that spends a second before its first await shows nothing
        if the first action is left to a background task."""
        update, bot = _fake_update()
        seen: list[int] = []

        async def handler(_update, _context):
            seen.append(bot.send_chat_action.await_count)

        await tb.with_typing(handler)(update, MagicMock())

        assert seen == [1]
        assert bot.send_chat_action.await_args.args == (456, ChatAction.TYPING)

    @pytest.mark.asyncio
    async def test_it_is_re_sent_while_a_slow_handler_is_still_running(self) -> None:
        """Telegram expires the action after about five seconds."""
        update, bot = _fake_update()

        async def handler(_update, _context):
            await asyncio.sleep(0.05)

        with patch.object(tb, "TYPING_REFRESH_S", 0.001):
            await tb.with_typing(handler)(update, MagicMock())

        assert bot.send_chat_action.await_count > 1

    @pytest.mark.asyncio
    async def test_it_stops_once_the_handler_returns(self) -> None:
        """A refresh left running keeps the indicator up over a delivered reply."""
        update, bot = _fake_update()

        async def handler(_update, _context):
            return None

        with patch.object(tb, "TYPING_REFRESH_S", 0.001):
            await tb.with_typing(handler)(update, MagicMock())
            after_return = bot.send_chat_action.await_count
            await asyncio.sleep(0.02)

        assert bot.send_chat_action.await_count == after_return

    @pytest.mark.asyncio
    async def test_the_handler_result_is_handed_back_unchanged(self) -> None:
        """The wrapper is in the registration path for every handler."""
        update, _bot = _fake_update()

        async def handler(_update, _context):
            return "conversation-state"

        assert await tb.with_typing(handler)(update, MagicMock()) == "conversation-state"

    @pytest.mark.asyncio
    async def test_a_telegram_that_refuses_the_action_does_not_fail_the_turn(self) -> None:
        """A dropped indicator is cosmetic; a dropped answer is not."""
        update, bot = _fake_update()
        bot.send_chat_action = AsyncMock(side_effect=RuntimeError("429"))

        async def handler(_update, _context):
            return "answered anyway"

        assert await tb.with_typing(handler)(update, MagicMock()) == "answered anyway"

    @pytest.mark.asyncio
    async def test_an_update_carrying_no_chat_still_reaches_its_handler(self) -> None:
        """There is nowhere to show an indicator, which is not a reason to drop
        the update."""
        update = MagicMock()
        update.effective_chat = None
        ran = []

        async def handler(_update, _context):
            ran.append(True)

        await tb.with_typing(handler)(update, MagicMock())

        assert ran == [True]


class TestEveryRegisteredHandlerHoldsIt:
    """The table is not the registration. Only the built application is."""

    def _registered_callbacks(self):
        with patch.object(tb, "_on_startup"), patch.object(tb, "_on_shutdown"):
            app = tb._build_application("123456:fake-token-for-tests")
        return [h.callback for group in app.handlers.values() for h in group]

    @pytest.mark.asyncio
    async def test_a_bare_registration_would_show_nothing_and_is_caught(self) -> None:
        """Every callback the application routes to sends a typing action.

        A handler added to `_build_application` without the wrapper is the
        regression: the command works, the reply arrives, and the chat sits
        blank for however long the gateway takes.
        """
        callbacks = self._registered_callbacks()
        assert callbacks, "nothing was registered — the walk below proves nothing"

        silent = []
        for callback in callbacks:
            update, bot = _fake_update()
            with patch.object(tb, "TYPING_REFRESH_S", 30):
                try:
                    await callback(update, MagicMock())
                except Exception:
                    pass  # the handler body is not what this guards
            if not bot.send_chat_action.await_count:
                silent.append(getattr(callback, "__name__", repr(callback)))

        assert silent == []
