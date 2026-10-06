"""A channel that exists for one conversation with one mind.

An ordinary guild channel is a room full of people talking to each other, so
the bot needs an explicit mention. A resident channel is the opposite case:
requiring a mention on every reply would make the continuity the channel was
created for cost a keystroke nobody keeps paying.
"""

from types import SimpleNamespace

import pytest

from hive_surfaces.config import SurfaceConfig, config, configure
from hive_surfaces.discord_bot import (
    conversation_channel_id,
    should_handle_message,
    task_channels,
)


@pytest.fixture(autouse=True)
def _restore_config():
    before = config._installed()
    yield
    configure(before)


class TestWhichChannelsAreResident:
    def test_the_configured_ids_are_read_as_ints(self) -> None:
        """YAML hands back strings often enough to matter."""
        configure(SurfaceConfig(discord_task_channels=["17", 23]))

        assert task_channels() == {17, 23}

    def test_no_configured_channels_means_none_are_resident(self) -> None:
        configure(SurfaceConfig())

        assert task_channels() == set()


class TestWhetherTheMessageIsAddressedToThisMind:
    def test_a_dm_never_needs_a_mention(self) -> None:
        assert should_handle_message(
            is_dm=True, mentioned=False, channel_id=1, task_channel_ids=set()
        )

    def test_an_ordinary_guild_channel_needs_one(self) -> None:
        assert not should_handle_message(
            is_dm=False, mentioned=False, channel_id=1, task_channel_ids={99}
        )
        assert should_handle_message(
            is_dm=False, mentioned=True, channel_id=1, task_channel_ids={99}
        )

    def test_a_resident_channel_does_not(self) -> None:
        assert should_handle_message(
            is_dm=False, mentioned=False, channel_id=99, task_channel_ids={99}
        )


class TestWhichConversationAThreadBelongsTo:
    def test_a_thread_under_a_resident_channel_folds_into_it(self) -> None:
        """Otherwise the reply starts a conversation holding none of its history."""
        thread = SimpleNamespace(id=555, parent_id=99)

        assert conversation_channel_id(thread, {99}) == 99

    def test_a_thread_in_an_ordinary_channel_keeps_its_own_id(self) -> None:
        """A thread in an ordinary channel is genuinely its own topic."""
        thread = SimpleNamespace(id=555, parent_id=12)

        assert conversation_channel_id(thread, {99}) == 555

    def test_a_channel_with_no_parent_is_its_own_conversation(self) -> None:
        channel = SimpleNamespace(id=99, parent_id=None)

        assert conversation_channel_id(channel, {99}) == 99
