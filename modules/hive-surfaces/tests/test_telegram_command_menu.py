"""The slash menu is published, and cannot be published malformed.

Requirement 1: Telegram's slash menu lists the mind's commands with a
description each, so a command can be tapped rather than remembered.

The menu and the handlers come from one table, so they cannot drift apart and
nothing here asserts that they match. What can still go wrong is a single
malformed entry: `set_my_commands` rejects the whole call over one bad name or
one over-long description, which takes down the menu for *every* command while
the bot carries on running. That failure is invisible from the inside — the
symptom is a phone showing no hints, which is the state before this feature
existed.
"""

import pytest


# Telegram's documented rules for a command name: 1-32 characters, lowercase
# letters, digits and underscores only. Hardcoded rather than imported from the
# module under test — importing the limit would make this pass for whatever
# value the module happened to hold.
_ALLOWED_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_")


class TestCommandMenu:
    def test_every_command_name_is_one_telegram_accepts(self) -> None:
        """Requirement 1: no entry can make set_my_commands reject the batch."""
        from hive_surfaces.telegram_bot import COMMANDS

        assert COMMANDS, "the menu table is empty — nothing would be published"
        for name, _description, _handler in COMMANDS:
            assert 1 <= len(name) <= 32, f"{name!r} is not 1-32 characters"
            assert set(name) <= _ALLOWED_NAME_CHARS, (
                f"{name!r} has characters Telegram rejects in a command name"
            )

    def test_every_command_has_a_description_within_the_limit(self) -> None:
        """Requirement 1: a tappable entry that explains itself."""
        from hive_surfaces.telegram_bot import COMMANDS

        for name, description, _handler in COMMANDS:
            assert description.strip(), f"{name} has no description to show"
            assert len(description) <= 256, f"{name}'s description exceeds 256 chars"

    def test_every_command_is_wired_to_something_callable(self) -> None:
        """Requirement 1: the menu never offers a command with no handler.

        The table feeds `add_handler` directly, so an entry whose third element
        is not callable takes the whole bot down at startup rather than
        offering a dead menu row. Caught here instead.
        """
        from hive_surfaces.telegram_bot import COMMANDS

        for name, _description, handler in COMMANDS:
            assert callable(handler), f"/{name} is in the menu with no handler"

    def test_rename_is_published(self) -> None:
        """Requirement 1: the command the operator asked for is on the menu.

        Named specifically because `/rename` is the command that prompted this
        work: it was reachable only by typing it with an argument.
        """
        from hive_surfaces.telegram_bot import COMMANDS

        assert "rename" in {name for name, _d, _h in COMMANDS}

    @pytest.mark.asyncio
    async def test_startup_schedules_the_menu_and_does_not_block_on_it(self) -> None:
        """Requirement 1: the menu is published, and never at the cost of booting.

        Publication runs as a background task — it retries for minutes, and a
        mind that waited for that before accepting its first message would be
        down for the duration. So startup schedules it and returns; what the
        task itself does is covered below.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        import hive_surfaces.telegram_bot as tb

        app = MagicMock()
        app.bot.set_my_commands = AsyncMock(side_effect=RuntimeError("rejected"))

        with patch.dict("os.environ", {"MIND_ID": "test-mind"}), \
                patch.object(tb, "aiohttp") as aio, \
                patch.object(tb, "GatewayClient", MagicMock()), \
                patch.object(tb, "_MENU_RETRY_S", 0):
            aio.ClientSession = MagicMock()
            aio.ClientTimeout = MagicMock()
            await tb._on_startup(app)
            assert tb._menu_task is not None
            await tb._menu_task

        # Boot survived a menu Telegram would not take, and the attempt was
        # made rather than skipped.
        app.bot.set_my_commands.assert_awaited()


class TestWhatIsActuallyPublished:
    """The table is not the menu. Publishing is what the operator sees.

    Every test above inspects `COMMANDS`, and a mutation run proved all of
    them pass with `command_menu()` returning an empty list — which is
    precisely the regression that started this work: sixteen handlers
    registered and `getMyCommands` returning nothing.
    """

    def test_the_published_menu_carries_every_command_in_the_table(self) -> None:
        """Requirement 1: what is published is what the table says."""
        from hive_surfaces.telegram_bot import COMMANDS, valid_menu_entries

        assert [name for name, _ in valid_menu_entries()] == [
            name for name, _d, _h in COMMANDS
        ]

    def test_a_malformed_entry_costs_only_itself(self) -> None:
        """Requirement 1: one bad entry must not take the whole menu down.

        `set_my_commands` rejects the entire batch over a single invalid name,
        so passing the table through unchecked means one typo silently removes
        every hint the operator has.
        """
        from unittest.mock import patch

        import hive_surfaces.telegram_bot as tb

        broken = (("Rename", "capitals are rejected by Telegram", print),) + tb.COMMANDS
        with patch.object(tb, "COMMANDS", broken):
            published = [name for name, _ in tb.valid_menu_entries()]

        assert "Rename" not in published
        assert published == [name for name, _d, _h in tb.COMMANDS]

    def test_an_over_long_description_is_dropped_not_published(self) -> None:
        """Requirement 1: the 256-char ceiling is enforced in the product."""
        from unittest.mock import patch

        import hive_surfaces.telegram_bot as tb

        broken = (("verbose", "x" * 257, print),) + tb.COMMANDS
        with patch.object(tb, "COMMANDS", broken):
            assert "verbose" not in [name for name, _ in tb.valid_menu_entries()]

    @pytest.mark.asyncio
    async def test_publishing_is_retried_after_a_failure(self) -> None:
        """Requirement 1: booting during an outage must not empty the menu forever.

        A single attempt at boot is published into whatever the network happens
        to be doing that second — and the outage this work exists to survive is
        exactly when a restart is likely.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        import hive_surfaces.telegram_bot as tb

        app = MagicMock()
        app.bot.set_my_commands = AsyncMock(side_effect=[RuntimeError("502"), None])

        with patch.object(tb, "_MENU_RETRY_S", 0):
            await tb._publish_command_menu(app)

        assert app.bot.set_my_commands.await_count == 2

    @pytest.mark.asyncio
    async def test_publishing_gives_up_rather_than_retrying_forever(self) -> None:
        """Requirement 1: an unpublishable menu is inconvenient, not a spin."""
        from unittest.mock import AsyncMock, MagicMock, patch

        import hive_surfaces.telegram_bot as tb

        app = MagicMock()
        app.bot.set_my_commands = AsyncMock(side_effect=RuntimeError("502"))

        with patch.object(tb, "_MENU_RETRY_S", 0):
            await tb._publish_command_menu(app)

        assert app.bot.set_my_commands.await_count == tb._MENU_ATTEMPTS
