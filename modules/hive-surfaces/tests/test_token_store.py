"""The token a surface authenticates with, replaced but never read.

Written where this host's surface reads it — the mind's own `.env` on an edge
install, a per-mind keyring key in the containerised stack — by the same
precedence `_get_bot_token` applies, which is why both live here. A writer
that disagreed with the reader would store a token in a place nothing
consults, and the page would still say saved.

Verified first, because a token with a character missing is accepted by any
file and arrives at the next restart as a surface that 401s on every poll and
reads as an outage.
"""

import sys
from unittest.mock import MagicMock

import pytest

from hive_surfaces import token_store as surface_token


@pytest.fixture()
def vault(monkeypatch):
    """A stand-in for the keyring. The vault is where our process ends."""
    stored: dict[tuple[str, str], str] = {}
    fake = MagicMock()
    fake.get_password = lambda service, key: stored.get((service, key))
    fake.set_password = lambda service, key, value: stored.__setitem__((service, key), value)
    monkeypatch.setitem(sys.modules, "keyring", fake)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_KEYRING_KEY", raising=False)
    return stored


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_PROJECT_DIR", str(tmp_path))
    return tmp_path


def _bot_api(username="hivemind_test_bot", ok=True, status=200, description=""):
    """A stand-in for the bot API — the network is where our process ends."""
    class _Response:
        def __init__(self):
            self.status = status

        async def json(self, content_type=None):
            if ok:
                return {"ok": True, "result": {"username": username, "is_bot": True}}
            return {"ok": False, "description": description or "Unauthorized"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    session = MagicMock()
    session.get = MagicMock(return_value=_Response())
    session.asked = session.get
    return session


GOOD = "8394092434:AAEhsomethingthatlookslikeatokenXYZ123"
# What a mind is holding before the write. Invented, like GOOD above — every
# token in this file is a shape, never a value anything would accept.
PREVIOUS = "1111111111:ZZwhateverwasthereagoodwhileago0000"


class TestWhereTheTokenLands:
    def test_a_named_keyring_key_is_where_the_stack_stores_it(
        self, vault, monkeypatch
    ) -> None:
        """Several surfaces run from one image there, and the environment
        holds one value per name."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN_KEYRING_KEY", "CYPHER_TELEGRAM_BOT_TOKEN")

        assert surface_token.storage_location() == ("keyring", "CYPHER_TELEGRAM_BOT_TOKEN")

    def test_with_no_named_key_it_is_the_mind_s_own_env(self, vault) -> None:
        assert surface_token.storage_location() == ("env", "TELEGRAM_BOT_TOKEN")

    @pytest.mark.asyncio
    async def test_a_verified_token_is_written_under_the_named_key(
        self, vault, monkeypatch
    ) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN_KEYRING_KEY", "CYPHER_TELEGRAM_BOT_TOKEN")

        await surface_token.replace(GOOD, session=_bot_api())

        assert vault[("hive-mind", "CYPHER_TELEGRAM_BOT_TOKEN")] == GOOD

    @pytest.mark.asyncio
    async def test_a_verified_token_is_written_into_the_env_file(
        self, vault, project
    ) -> None:
        (project / ".env").write_text(  # secret-guard: allow — invented
            "# this mind\nMIND_NAME=edge\n"
            "TELEGRAM_BOT_TOKEN=" + PREVIOUS + "\nOTHER=keep\n"
        )

        await surface_token.replace(GOOD, session=_bot_api())

        text = (project / ".env").read_text()
        assert f"TELEGRAM_BOT_TOKEN={GOOD}\n" in text
        assert PREVIOUS not in text
        assert "# this mind" in text and "OTHER=keep" in text

    @pytest.mark.asyncio
    async def test_an_env_file_without_the_line_gains_one(
        self, vault, project
    ) -> None:
        (project / ".env").write_text("MIND_NAME=edge\n")

        await surface_token.replace(GOOD, session=_bot_api())

        assert f"TELEGRAM_BOT_TOKEN={GOOD}" in (project / ".env").read_text()

    @pytest.mark.asyncio
    async def test_setting_twice_replaces_rather_than_accumulates(
        self, vault, project
    ) -> None:
        """Two declarations of one name is a file whose meaning depends on
        which end the reader starts from."""
        second = "8394092434:BBfreshtokenafterarotationZZZ98765"

        await surface_token.replace(GOOD, session=_bot_api())
        await surface_token.replace(second, session=_bot_api())

        text = (project / ".env").read_text()
        assert text.count("TELEGRAM_BOT_TOKEN=") == 1
        assert second in text

    @pytest.mark.asyncio
    async def test_the_env_file_keeps_its_mode(self, vault, project) -> None:
        """A fresh inode at 0644 is a mind's secrets readable by anyone on
        the host."""
        env = project / ".env"
        env.write_text("TELEGRAM_BOT_TOKEN=" + PREVIOUS + "\n")
        env.chmod(0o600)

        await surface_token.replace(GOOD, session=_bot_api())

        assert env.stat().st_mode & 0o777 == 0o600


class TestWhatIsRefused:
    @pytest.mark.asyncio
    async def test_a_token_the_bot_api_rejects_is_not_stored(
        self, vault, project
    ) -> None:
        """The operator pasting a typo has not asked to take the surface
        down, so the working token stays."""
        (project / ".env").write_text("TELEGRAM_BOT_TOKEN=" + PREVIOUS + "\n")

        with pytest.raises(surface_token.TokenRefused):
            await surface_token.replace(GOOD, session=_bot_api(ok=False, status=401))

        assert PREVIOUS in (project / ".env").read_text()

    @pytest.mark.asyncio
    async def test_the_bot_api_s_own_reason_reaches_the_caller(self, vault) -> None:
        with pytest.raises(surface_token.TokenRefused, match="Unauthorized"):
            await surface_token.verify(GOOD, session=_bot_api(ok=False, status=401))

    @pytest.mark.asyncio
    async def test_something_that_is_not_a_token_never_reaches_the_bot_api(
        self, vault
    ) -> None:
        """An obvious paste error is not worth handing to an external
        service."""
        session = _bot_api()

        with pytest.raises(surface_token.TokenRefused):
            await surface_token.verify("hunter2", session=session)

        session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_unreachable_bot_api_refuses_rather_than_storing_blind(
        self, vault, project
    ) -> None:
        session = MagicMock()
        session.get = MagicMock(side_effect=OSError("dns"))

        with pytest.raises(surface_token.TokenRefused):
            await surface_token.replace(GOOD, session=session)

        assert not (project / ".env").exists()


class TestWhatTheConsoleMayKnow:
    @pytest.mark.asyncio
    async def test_a_mind_with_no_token_reports_absent(self, vault, project) -> None:
        state = await surface_token.status(session=_bot_api())

        assert (state.stored, state.accepted) == (False, None)

    @pytest.mark.asyncio
    async def test_a_stored_token_the_api_rejects_is_not_the_same_as_absent(
        self, vault, project
    ) -> None:
        """One is a token nobody supplied; the other is one that was revoked
        and is failing on every poll behind a surface that still looks up."""
        (project / ".env").write_text(f"TELEGRAM_BOT_TOKEN={GOOD}\n")

        state = await surface_token.status(session=_bot_api(ok=False, status=401))

        assert (state.stored, state.accepted) == (True, False)

    @pytest.mark.asyncio
    async def test_an_accepted_token_is_reported_with_the_bot_it_belongs_to(
        self, vault, project
    ) -> None:
        """So the operator can see it is the mind they meant."""
        (project / ".env").write_text(f"TELEGRAM_BOT_TOKEN={GOOD}\n")

        state = await surface_token.status(session=_bot_api(username="hivemind_cypher_bot"))

        assert state.accepted is True
        assert state.bot_username == "hivemind_cypher_bot"

    @pytest.mark.asyncio
    async def test_nothing_reported_carries_the_token_itself(
        self, vault, project
    ) -> None:
        """The operator needs to know it works, not what it is."""
        (project / ".env").write_text(f"TELEGRAM_BOT_TOKEN={GOOD}\n")

        state = await surface_token.status(session=_bot_api())

        assert GOOD not in repr(state)

    @pytest.mark.asyncio
    async def test_a_token_written_since_boot_is_what_is_reported(
        self, vault, project, monkeypatch
    ) -> None:
        """`os.environ` is a snapshot from startup, so reading it would tell
        the operator their write never landed."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", PREVIOUS)
        (project / ".env").write_text(f"TELEGRAM_BOT_TOKEN={GOOD}\n")

        assert surface_token.stored_token() == GOOD


class TestTheFileTheConsoleAlsoHolds:
    """An edge mind's `.env` is bind-mounted into the hive console as a single
    file. A rename hands the host a new inode while that mount goes on
    pointing at the old one, so from that moment the console reads and rotates
    a file nobody loads — five green ticks over a mind that 401s on every call
    after its next restart.
    """

    @pytest.mark.asyncio
    async def test_the_env_file_keeps_its_inode(self, vault, project) -> None:
        env = project / ".env"
        env.write_text("TELEGRAM_BOT_TOKEN=" + PREVIOUS + "\nOTHER=keep\n")
        before = env.stat().st_ino

        await surface_token.replace(GOOD, session=_bot_api())

        assert env.stat().st_ino == before
        assert GOOD in env.read_text()

    @pytest.mark.asyncio
    async def test_a_write_that_fails_partway_leaves_the_file_as_it_was(
        self, vault, project, monkeypatch
    ) -> None:
        """Truncate-then-write means a failure leaves an empty `.env`, which
        is a mind that will not boot."""
        env = project / ".env"
        original = "TELEGRAM_BOT_TOKEN=" + PREVIOUS + "\nOTHER=keep\n"
        env.write_text(original)

        real_open = open
        calls = {"n": 0}

        def failing_open(path, mode="r", *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            if "w" in mode and str(path) == str(env):
                calls["n"] += 1
                if calls["n"] == 1:
                    class _Failing:
                        def __enter__(self_inner):
                            return self_inner

                        def __exit__(self_inner, *_):
                            handle.close()
                            return False

                        def write(self_inner, _text):
                            raise OSError("no space left on device")

                    return _Failing()
            return handle

        monkeypatch.setattr("builtins.open", failing_open)

        with pytest.raises(OSError):
            await surface_token.replace(GOOD, session=_bot_api())

        monkeypatch.undo()
        assert env.read_text() == original


class TestWhatAnUpstreamQuotesBack:
    @pytest.mark.asyncio
    async def test_an_error_carrying_the_verify_url_is_redacted(self, vault) -> None:
        """The bot API's own shape puts the token in the URL, and aiohttp
        quotes that URL in the text of several of its errors — text the mind
        logs and returns to the console."""
        session = MagicMock()
        session.get = MagicMock(
            side_effect=RuntimeError(f"unexpected mimetype, url='bot{GOOD}/getMe'")
        )

        with pytest.raises(surface_token.TokenRefused) as refusal:
            await surface_token.verify(GOOD, session=session)

        assert str(refusal.value) == (
            "could not reach the bot API: unexpected mimetype, url='bot<token>/getMe'"
        )
