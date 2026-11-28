"""Which token a surface starts with, when one image runs several bots.

An edge mind has one bot per host and puts its token in its own `.env`. A
stack runs several surfaces from one image on one machine, where the
environment cannot hold several different values under one name — so each
names its own keyring key. Reading only the environment there starts every
bot on whichever token happened to be exported, and three bots polling
Telegram as one bot steal each other's updates with nothing in any log.
"""

import sys
from unittest.mock import MagicMock

import pytest

import hive_surfaces.telegram_bot as tb
from hive_surfaces import token_store


@pytest.fixture()
def vault(monkeypatch):
    """A stand-in for the keyring — the vault is where our process ends."""
    stored: dict[tuple[str, str], str] = {}
    fake = MagicMock()
    fake.get_password = lambda service, key: stored.get((service, key))
    monkeypatch.setitem(sys.modules, "keyring", fake)
    # Every name both surfaces read, cleared — not just Telegram's. The host
    # running this suite is a host that runs minds, so its own environment
    # holds real tokens, and a test that left one visible would assert
    # against a live credential and print it on failure.
    for surface in ("telegram", "discord"):
        for name in token_store.names(surface):
            monkeypatch.delenv(name, raising=False)
    return stored


class TestWhichTokenASurfaceStartsOn:
    def test_a_named_key_beats_an_ambient_environment_token(
        self, vault, monkeypatch
    ) -> None:
        """The only reason to name a key is that the ambient one is not yours."""
        vault[(tb.KEYRING_SERVICE, "BILBY_TELEGRAM_BOT_TOKEN")] = "bilby-token"
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "some-other-minds-token")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN_KEYRING_KEY", "BILBY_TELEGRAM_BOT_TOKEN")

        assert tb._get_bot_token() == "bilby-token"

    def test_with_no_named_key_the_environment_wins(self, vault, monkeypatch) -> None:
        """An edge mind's own `.env` is the answer for that host."""
        vault[(tb.KEYRING_SERVICE, tb.DEFAULT_TOKEN_KEY)] = "stale-keyring-copy"
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")

        assert tb._get_bot_token() == "env-token"

    def test_with_nothing_in_the_environment_the_default_key_is_read(
        self, vault
    ) -> None:
        """Which is how the stack's first surface has always been configured."""
        vault[(tb.KEYRING_SERVICE, tb.DEFAULT_TOKEN_KEY)] = "keyring-token"

        assert tb._get_bot_token() == "keyring-token"

    def test_a_named_key_with_nothing_under_it_falls_back_to_the_environment(
        self, vault, monkeypatch
    ) -> None:
        """A key named before the token was stored must not be fatal when the
        host has one to offer."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN_KEYRING_KEY", "CYPHER_TELEGRAM_BOT_TOKEN")

        assert tb._get_bot_token() == "env-token"

    def test_a_keyring_that_cannot_be_read_does_not_cost_a_working_token(
        self, monkeypatch
    ) -> None:
        """A host with no backend installed is not a host with no token."""
        broken = MagicMock()
        broken.get_password = MagicMock(side_effect=RuntimeError("no backend"))
        monkeypatch.setitem(sys.modules, "keyring", broken)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN_KEYRING_KEY", raising=False)

        assert tb._get_bot_token() == "env-token"

    def test_no_token_anywhere_stops_rather_than_starting_tokenless(
        self, vault
    ) -> None:
        """Starting is worse than exiting: the surface would log a 401 per
        poll forever and look like Telegram was down."""
        with pytest.raises(SystemExit):
            tb._get_bot_token()

    def test_a_named_key_with_nothing_anywhere_stops_too(
        self, vault, monkeypatch
    ) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN_KEYRING_KEY", "CYPHER_TELEGRAM_BOT_TOKEN")

        with pytest.raises(SystemExit):
            tb._get_bot_token()


class TestTheRuleCoversBothSurfaces:
    """Discord was left reading only the environment when Telegram's keyring
    path was restored, and a mind whose Discord token was in the keyring came
    up crashlooping on a token it had all along. One table, both surfaces.
    """

    def test_discord_reads_the_keyring_under_its_own_default_key(
        self, vault
    ) -> None:
        import hive_surfaces.discord_bot as db

        vault[(tb.KEYRING_SERVICE, "DISCORD_BOT_TOKEN")] = "discord-token"

        assert db._get_bot_token() == "discord-token"

    def test_discord_honours_a_named_key_of_its_own(self, vault, monkeypatch) -> None:
        import hive_surfaces.discord_bot as db

        vault[(tb.KEYRING_SERVICE, "ADA_DISCORD_BOT_TOKEN")] = "ada-discord-token"
        monkeypatch.setenv("DISCORD_BOT_TOKEN_KEYRING_KEY", "ADA_DISCORD_BOT_TOKEN")
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "some-other-minds-token")

        assert db._get_bot_token() == "ada-discord-token"

    def test_a_mind_with_no_discord_token_is_skipped_rather_than_stopped(
        self, vault, monkeypatch
    ) -> None:
        """A mind may legitimately run Telegram-only."""
        import hive_surfaces.discord_bot as db

        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.delenv("DISCORD_BOT_TOKEN_KEYRING_KEY", raising=False)

        assert db._get_bot_token() is None

    def test_the_two_surfaces_do_not_read_each_other_s_token(
        self, vault, monkeypatch
    ) -> None:
        import hive_surfaces.discord_bot as db

        vault[(tb.KEYRING_SERVICE, "TELEGRAM_BOT_TOKEN")] = "telegram-token"
        vault[(tb.KEYRING_SERVICE, "DISCORD_BOT_TOKEN")] = "discord-token"

        assert tb._get_bot_token() == "telegram-token"
        assert db._get_bot_token() == "discord-token"
        assert token_store.names("telegram") != token_store.names("discord")

    def test_an_unknown_surface_is_refused_rather_than_guessed(self, vault) -> None:
        """A typo must not resolve to whichever surface sorts first and hand
        one bot another's credential."""
        with pytest.raises(ValueError):
            token_store.resolve_token("slack")
