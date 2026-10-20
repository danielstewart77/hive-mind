"""The gate in front of every inbound message, once the claim is the authority.

These drive `telegram_bot._claim_or_refuse` and `discord_bot._claim_or_refuse`
themselves with a resolver installed, rather than patching the authorizer the
way the rest of this suite does: the authorizer is the behaviour under test
here, so standing anything in for it would leave nothing being tested.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hive_surfaces import discord_bot, surface_claim, telegram_bot
from hive_surfaces.config import SurfaceConfig, configure
from hive_surfaces.surface_claim import ClaimAnswer


class _Resolver:
    """A claim as the gateway would report it, without the round trip."""

    supported = True

    def __init__(self, claimed_ref: str = "", *, claims_on: str | None = None) -> None:
        self.claimed_ref = claimed_ref
        self.claims_on = claims_on
        self.offered: list[tuple[str, str]] = []

    async def is_allowed(self, user_id) -> bool:
        return bool(self.claimed_ref) and str(user_id) == self.claimed_ref

    async def is_claimed(self) -> bool:
        return bool(self.claimed_ref)

    async def attempt_claim(self, sender_ref, text: str) -> ClaimAnswer:
        self.offered.append((str(sender_ref), text))
        if self.claims_on is not None and text == self.claims_on:
            self.claimed_ref = str(sender_ref)
            return ClaimAnswer(True, "claimed", self.claimed_ref)
        return ClaimAnswer(False, "mismatch")


@pytest.fixture(autouse=True)
def _clean():
    configure(SurfaceConfig())
    surface_claim.forget_all()
    yield
    surface_claim.forget_all()
    configure(SurfaceConfig())


def _update(user_id: int, *, chat_type: str = "private", text: str = "hello"):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = 456
    update.effective_chat.type = chat_type
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


async def test_the_claimed_person_is_admitted():
    surface_claim.install(_Resolver("123"))
    update = _update(123)
    assert await telegram_bot._claim_or_refuse(update) is True
    update.message.reply_text.assert_not_awaited()


async def test_a_stranger_in_a_private_chat_is_told_plainly():
    surface_claim.install(_Resolver("123"))
    update = _update(999)
    assert await telegram_bot._claim_or_refuse(update) is False
    update.message.reply_text.assert_awaited_once_with("Not authorized.")


async def test_a_stranger_in_a_group_is_refused_without_a_word():
    surface_claim.install(_Resolver("123"))
    update = _update(999, chat_type="group")
    assert await telegram_bot._claim_or_refuse(update) is False
    update.message.reply_text.assert_not_awaited()


async def test_the_allow_list_no_longer_admits_anybody_once_a_claim_decides():
    configure(SurfaceConfig(telegram_allowed_users=[999]))
    surface_claim.install(_Resolver("123"))
    assert await telegram_bot._is_allowed_user(999) is False
    assert await telegram_bot._is_allowed_user(123) is True


async def test_a_process_handed_no_resolver_falls_back_to_the_allow_list():
    configure(SurfaceConfig(telegram_allowed_users=[999]))
    assert await telegram_bot._is_allowed_user(999) is True
    assert await telegram_bot._is_allowed_user(123) is False


async def test_the_phrase_sent_to_an_unclaimed_bot_claims_it():
    resolver = _Resolver("", claims_on="anchor basalt cinder")
    surface_claim.install(resolver)
    update = _update(777, text="anchor basalt cinder")
    assert await telegram_bot._claim_or_refuse(update) is False
    assert resolver.claimed_ref == "777"
    update.message.reply_text.assert_awaited_once()
    assert "yours" in update.message.reply_text.await_args.args[0]


async def test_the_next_message_after_a_claim_is_answered():
    resolver = _Resolver("", claims_on="anchor basalt cinder")
    surface_claim.install(resolver)
    await telegram_bot._claim_or_refuse(_update(777, text="anchor basalt cinder"))
    assert await telegram_bot._claim_or_refuse(_update(777, text="hello")) is True


async def test_a_claimed_bot_offers_nothing_for_judgement():
    resolver = _Resolver("123", claims_on="anchor basalt cinder")
    surface_claim.install(resolver)
    await telegram_bot._claim_or_refuse(_update(999, text="anchor basalt cinder"))
    assert resolver.offered == []
    assert resolver.claimed_ref == "123"


async def test_an_unclaimed_bot_is_not_claimable_from_a_group():
    resolver = _Resolver("", claims_on="anchor basalt cinder")
    surface_claim.install(resolver)
    update = _update(777, chat_type="group", text="anchor basalt cinder")
    assert await telegram_bot._claim_or_refuse(update) is False
    assert resolver.offered == []
    update.message.reply_text.assert_not_awaited()


def _discord_message(user_id: int, *, dm: bool, content: str = "hello"):
    import discord

    message = MagicMock()
    message.author.id = user_id
    message.content = content
    message.channel = MagicMock(spec=discord.DMChannel) if dm else MagicMock()
    message.channel.id = 456
    message.channel.send = AsyncMock()
    return message


async def test_discord_admits_only_the_claimed_person():
    surface_claim.install(_Resolver("123"), "discord")
    assert await discord_bot._claim_or_refuse(_discord_message(123, dm=True)) is True


async def test_discord_says_nothing_to_a_stranger_in_a_channel():
    surface_claim.install(_Resolver("123"), "discord")
    message = _discord_message(999, dm=False)
    assert await discord_bot._claim_or_refuse(message) is False
    message.channel.send.assert_not_awaited()


async def test_discord_is_claimed_by_a_direct_message_carrying_the_phrase():
    resolver = _Resolver("", claims_on="anchor basalt cinder")
    surface_claim.install(resolver, "discord")
    message = _discord_message(777, dm=True, content="anchor basalt cinder")
    assert await discord_bot._claim_or_refuse(message) is False
    assert resolver.claimed_ref == "777"
    message.channel.send.assert_awaited_once()


class _UnsupportedResolver(_Resolver):
    """A gateway that predates the claim routes."""

    supported = False


async def test_an_old_gateway_leaves_the_allow_list_in_charge():
    configure(SurfaceConfig(telegram_allowed_users=[999]))
    surface_claim.install(_UnsupportedResolver(""))
    assert await telegram_bot._is_allowed_user(999) is True
    assert await telegram_bot._is_allowed_user(123) is False


async def test_an_old_gateway_is_never_offered_a_claim():
    resolver = _UnsupportedResolver("", claims_on="anchor basalt cinder")
    configure(SurfaceConfig(telegram_allowed_users=[999]))
    surface_claim.install(resolver)
    update = _update(777, text="anchor basalt cinder")
    assert await telegram_bot._claim_or_refuse(update) is False
    assert resolver.offered == []


async def test_a_person_who_already_holds_the_bot_has_their_message_answered():
    """A stale cache is not a refusal.

    The gateway says the claim is already theirs; answering that with a
    claim confirmation would drop the message they actually typed.
    """
    class _Stale(_Resolver):
        async def is_claimed(self) -> bool:
            return False

        async def attempt_claim(self, sender_ref, text):
            from hive_surfaces.surface_claim import ClaimAnswer
            self.offered.append((str(sender_ref), text))
            return ClaimAnswer(False, "already_yours", str(sender_ref))

    surface_claim.install(_Stale(""))
    update = _update(777, text="what is on today")

    assert await telegram_bot._claim_or_refuse(update) is True
    update.message.reply_text.assert_not_awaited()


async def test_an_edited_message_from_a_stranger_does_not_raise():
    """An edit arrives with no message at all, and a refusal that raised
    inside the handler would be silence."""
    surface_claim.install(_Resolver("123"))
    update = MagicMock()
    update.effective_user.id = 999
    update.effective_chat.id = 456
    update.effective_chat.type = "private"
    update.message = None

    assert await telegram_bot._claim_or_refuse(update) is False
