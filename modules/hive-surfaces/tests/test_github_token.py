"""The GitHub token a mind pushes with, verified then stored where it is read.

Stored in the keyring on a containerised mind, because its home directory is
image layers a rebuild deletes and the keyring is a bind mount — and written
*from* there into the two files `git` and `gh` actually read, since neither
consults a keyring and both read their file per invocation. That pair is what
makes a stored token usable without the mind being restarted, and what a
rebuilt container gets back before anything asks.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

from hive_surfaces import github_token

# Invented shapes, never values anything would accept.
GOOD = "ghp_AAbbCCddEEffGGhhIIjjKKllMMnnOOpp1234"
PREVIOUS = "ghp_ZZyyXXwwVVuuTTssRRqqPPooNNmmLL9876"


@pytest.fixture()
def vault(monkeypatch):
    """A stand-in for the keyring. The vault is where our process ends."""
    stored: dict[tuple[str, str], str] = {}
    fake = MagicMock()
    fake.get_password = lambda service, key: stored.get((service, key))
    fake.set_password = lambda service, key, value: stored.__setitem__(
        (service, key), value
    )
    monkeypatch.setitem(sys.modules, "keyring", fake)
    monkeypatch.delenv("GITHUB_TOKEN_KEYRING_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return stored


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A home directory. Whose it is, is the next fixture's business."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KEY_RING", str(tmp_path / "keyring"))
    monkeypatch.delenv("GITHUB_TOKEN_CONFIGURES_TOOLS", raising=False)
    (tmp_path / "home").mkdir()
    # `git config --global` would otherwise edit the real one.
    monkeypatch.setattr(github_token.subprocess, "run", MagicMock())
    return tmp_path / "home"


@pytest.fixture()
def container(home, monkeypatch):
    """A mind whose home is image layers nothing else reads.

    Both halves, because they are two different facts: where the token is
    stored, and whether this mind's home is its own to rewrite.
    """
    monkeypatch.setenv("GITHUB_TOKEN_KEYRING_KEY", "CYPHER_GITHUB_TOKEN")
    monkeypatch.setenv("GITHUB_TOKEN_CONFIGURES_TOOLS", "1")
    return home


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_PROJECT_DIR", str(tmp_path))
    return tmp_path


def _github(login="danielstewart77", status=200, raises=None):
    """A stand-in for the GitHub API — the network is where our process ends."""

    class _Response:
        def __init__(self):
            self.status = status

        async def json(self, content_type=None):
            return {"login": login}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    session = MagicMock()
    if raises is not None:
        session.get = MagicMock(side_effect=raises)
    else:
        session.get = MagicMock(return_value=_Response())
    return session


class TestWhereTheTokenLands:
    def test_a_named_keyring_key_is_where_the_stack_stores_it(
        self, vault, monkeypatch
    ) -> None:
        """Several minds run from one image on one machine, and the
        environment holds one value per name."""
        monkeypatch.setenv("GITHUB_TOKEN_KEYRING_KEY", "CYPHER_GITHUB_TOKEN")

        assert github_token.storage_location() == ("keyring", "CYPHER_GITHUB_TOKEN")

    def test_with_no_named_key_it_is_the_mind_s_own_env(self, vault) -> None:
        assert github_token.storage_location() == ("env", "GITHUB_TOKEN")

    @pytest.mark.asyncio
    async def test_a_verified_token_is_written_under_the_named_key(
        self, vault, container
    ) -> None:
        await github_token.replace(GOOD, session=_github())

        assert vault[("hive-mind", "CYPHER_GITHUB_TOKEN")] == GOOD

    @pytest.mark.asyncio
    async def test_the_keyring_store_also_writes_the_files_git_and_gh_read(
        self, vault, container
    ) -> None:
        """A keyring is not a thing `git` or `gh` consults, and both read
        their file per invocation — which is what makes a stored token usable
        without the mind being restarted."""
        await github_token.replace(GOOD, session=_github())

        assert GOOD in (container / ".git-credentials").read_text()
        assert GOOD in (container / ".config" / "gh" / "hosts.yml").read_text()

    @pytest.mark.asyncio
    async def test_the_files_git_and_gh_read_are_owner_only(
        self, vault, container
    ) -> None:
        """On a shared image, world-readable is every other mind."""
        await github_token.replace(GOOD, session=_github())

        assert (container / ".git-credentials").stat().st_mode & 0o777 == 0o600
        assert (container / ".config" / "gh" / "hosts.yml").stat().st_mode & 0o777 == 0o600

    @pytest.mark.asyncio
    async def test_the_git_credential_line_names_the_account_it_verified_as(
        self, vault, container
    ) -> None:
        await github_token.replace(GOOD, session=_github(login="cypher-bot"))

        assert (
            f"https://cypher-bot:{GOOD}@github.com"
            in (container / ".git-credentials").read_text()
        )

    @pytest.mark.asyncio
    async def test_an_edge_mind_stores_it_in_its_own_env_file(
        self, vault, project, home
    ) -> None:
        (project / ".env").write_text(
            "# this mind\nMIND_NAME=edge\nGITHUB_TOKEN=" + PREVIOUS + "\nOTHER=keep\n"
        )

        await github_token.replace(GOOD, session=_github())

        text = (project / ".env").read_text()
        assert f"GITHUB_TOKEN={GOOD}\n" in text
        assert PREVIOUS not in text
        assert "# this mind" in text and "OTHER=keep" in text

    @pytest.mark.asyncio
    async def test_an_edge_mind_leaves_the_operators_own_git_files_alone(
        self, vault, project, home
    ) -> None:
        """The home directory on an edge install is the operator's, their
        `~/.git-credentials` is a credential the console manages itself, and
        a mind rewriting it would put two writers on one file."""
        await github_token.replace(GOOD, session=_github())

        assert not (home / ".git-credentials").exists()


class TestWhatABootGetsBack:
    def test_a_rebuilt_container_gets_its_tool_files_from_the_keyring(
        self, vault, container
    ) -> None:
        """The keyring is a bind mount and survives; the home directory is
        image layers and does not. The first thing to notice otherwise would
        be a push failing."""
        vault[("hive-mind", "CYPHER_GITHUB_TOKEN")] = GOOD

        github_token.apply_stored()

        assert GOOD in (container / ".git-credentials").read_text()

    def test_an_edge_mind_writes_nothing_at_boot(self, vault, project, home) -> None:
        github_token.apply_stored()

        assert not (home / ".git-credentials").exists()

    def test_a_mind_with_no_stored_token_writes_no_tool_files(
        self, vault, container
    ) -> None:
        """An empty credentials file is a `git` that stops asking the helper
        it would otherwise fall through to."""
        github_token.apply_stored()

        assert not (container / ".git-credentials").exists()


class TestWhatIsRefused:
    @pytest.mark.asyncio
    async def test_a_token_github_rejects_is_not_stored(
        self, vault, container
    ) -> None:
        """The operator pasting a typo has not asked to take the mind's
        pushes down, so the working token stays."""
        vault[("hive-mind", "CYPHER_GITHUB_TOKEN")] = PREVIOUS

        with pytest.raises(github_token.TokenRefused):
            await github_token.replace(GOOD, session=_github(status=401))

        assert vault[("hive-mind", "CYPHER_GITHUB_TOKEN")] == PREVIOUS

    @pytest.mark.asyncio
    async def test_a_rejected_token_leaves_an_edge_minds_env_as_it_was(
        self, vault, project, home
    ) -> None:
        (project / ".env").write_text("GITHUB_TOKEN=" + PREVIOUS + "\n")

        with pytest.raises(github_token.TokenRefused):
            await github_token.replace(GOOD, session=_github(status=401))

        assert PREVIOUS in (project / ".env").read_text()

    @pytest.mark.asyncio
    async def test_something_that_cannot_be_sent_never_reaches_github(
        self, vault
    ) -> None:
        """A paste out of a browser carries zero-width spaces and smart
        quotes, which reach the header builder and raise there."""
        session = _github()

        with pytest.raises(github_token.TokenRefused):
            await github_token.verify("ghp_​notatoken", session=session)

        session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_unreachable_github_stores_nothing_and_refuses_no_token(
        self, vault, container
    ) -> None:
        """Nothing is stored, and the paste is not called bad: the verifier
        is what failed, and a page reading that as a refusal would have the
        operator revoking a token that works."""
        with pytest.raises(github_token.TokenUnverifiable):
            await github_token.replace(GOOD, session=_github(raises=OSError("dns")))

        assert ("hive-mind", "CYPHER_GITHUB_TOKEN") not in vault

    @pytest.mark.asyncio
    async def test_an_upstream_error_quoting_the_token_is_redacted(
        self, vault
    ) -> None:
        """This text is logged by the mind and returned to the console, and
        an upstream is free to quote whatever it was handed back at us."""
        session = _github(raises=RuntimeError(f"bad request with {GOOD} in it"))

        with pytest.raises(github_token.TokenUnverifiable) as refusal:
            await github_token.verify(GOOD, session=session)

        assert str(refusal.value) == "could not reach GitHub: bad request with <token> in it"


class TestWhatTheConsoleMayKnow:
    @pytest.mark.asyncio
    async def test_a_mind_with_no_token_reports_absent(
        self, vault, project, home
    ) -> None:
        state = await github_token.status(session=_github())

        assert (state.stored, state.accepted) == (False, None)

    @pytest.mark.asyncio
    async def test_a_stored_token_github_rejects_is_not_the_same_as_absent(
        self, vault, project, home
    ) -> None:
        """One is a token nobody supplied; the other is one revoked or pasted
        wrong, failing on every push behind a row that still looks set."""
        (project / ".env").write_text("GITHUB_TOKEN=" + PREVIOUS + "\n")

        state = await github_token.status(session=_github(status=401))

        assert (state.stored, state.accepted) == (True, False)

    @pytest.mark.asyncio
    async def test_a_working_token_is_named_by_the_account_it_belongs_to(
        self, vault, project, home
    ) -> None:
        """So the operator can see it is the account they meant."""
        (project / ".env").write_text("GITHUB_TOKEN=" + GOOD + "\n")

        state = await github_token.status(session=_github(login="danielstewart77"))

        assert (state.stored, state.accepted, state.login) == (
            True,
            True,
            "danielstewart77",
        )

    @pytest.mark.asyncio
    async def test_the_status_read_comes_off_disk_not_the_boot_environment(
        self, vault, project, home, monkeypatch
    ) -> None:
        """`os.environ` is a snapshot from boot, so a token written an hour
        ago would read as never having landed."""
        monkeypatch.setenv("GITHUB_TOKEN", PREVIOUS)
        (project / ".env").write_text("GITHUB_TOKEN=" + GOOD + "\n")

        assert github_token.stored_token() == GOOD

    @pytest.mark.asyncio
    async def test_the_row_says_where_the_token_is_kept(
        self, vault, container
    ) -> None:
        """Which keyring key or which file is the operator's whole remedy
        path."""
        await github_token.replace(GOOD, session=_github())
        state = await github_token.status(session=_github())

        assert state.where == "keyring:CYPHER_GITHUB_TOKEN"


class TestWhenGithubGivesNoVerdict:
    """A refusal and a non-answer are two situations with two remedies: one
    is a token to replace, the other a verifier to wait for. Folded together,
    the page tells the operator to revoke a token that works."""

    @pytest.mark.asyncio
    async def test_a_rate_limit_is_not_a_rejection(self, vault) -> None:
        """403 is how a secondary rate limit arrives, and it says nothing
        whatever about the token."""
        with pytest.raises(github_token.TokenUnverifiable):
            await github_token.verify(GOOD, session=_github(status=403))

    @pytest.mark.asyncio
    async def test_a_stored_token_with_no_verdict_is_neither_accepted_nor_refused(
        self, vault, project, home
    ) -> None:
        (project / ".env").write_text("GITHUB_TOKEN=" + GOOD + "\n")

        state = await github_token.status(session=_github(status=500))

        assert (state.stored, state.accepted) == (True, None)

    @pytest.mark.asyncio
    async def test_a_stored_token_github_rejects_is_still_refused(
        self, vault, project, home
    ) -> None:
        """The other side of the same boundary: 401 is a verdict."""
        (project / ".env").write_text("GITHUB_TOKEN=" + GOOD + "\n")

        state = await github_token.status(session=_github(status=401))

        assert (state.stored, state.accepted) == (True, False)


class TestWhatTheRowCanShow:
    @pytest.mark.asyncio
    async def test_a_stored_token_reports_its_last_four_characters(
        self, vault, project, home
    ) -> None:
        """Four characters tell the person who pasted it which token this is
        and authenticate as nobody."""
        (project / ".env").write_text("GITHUB_TOKEN=" + GOOD + "\n")

        state = await github_token.status(session=_github())

        assert state.preview == "..." + GOOD[-4:]

    @pytest.mark.asyncio
    async def test_a_short_value_is_starred_rather_than_mostly_shown(
        self, vault, project, home
    ) -> None:
        (project / ".env").write_text("GITHUB_TOKEN=abc\n")

        state = await github_token.status(session=_github(status=401))

        assert state.preview == "****"


class TestWhoseHomeItIs:
    @pytest.mark.asyncio
    async def test_a_keyring_key_alone_does_not_license_rewriting_a_home(
        self, vault, home, monkeypatch
    ) -> None:
        """An edge install copying the container's keyring pattern would
        otherwise have the mind truncating the operator's own
        `~/.git-credentials` and `gh` hosts file — two files the console
        already manages, whose other hosts a single-block rewrite destroys."""
        monkeypatch.setenv("GITHUB_TOKEN_KEYRING_KEY", "SKIPPY_GITHUB_TOKEN")

        await github_token.replace(GOOD, session=_github())

        assert vault[("hive-mind", "SKIPPY_GITHUB_TOKEN")] == GOOD
        assert not (home / ".git-credentials").exists()

    def test_a_boot_writes_no_home_it_does_not_own_either(
        self, vault, home, monkeypatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN_KEYRING_KEY", "SKIPPY_GITHUB_TOKEN")
        vault[("hive-mind", "SKIPPY_GITHUB_TOKEN")] = GOOD

        assert github_token.apply_stored() == []
        assert not (home / ".git-credentials").exists()


class TestWhenTheToolFilesCannotBeWritten:
    @pytest.mark.asyncio
    async def test_the_token_is_still_stored_and_the_failure_is_named(
        self, vault, container
    ) -> None:
        """By the time those files are written the token is in the keyring.
        Raising here would answer the caller with a failure over a token that
        did land, and the operator would paste it again or conclude it had
        not taken."""
        (container / ".git-credentials").mkdir()  # a directory where a file goes

        state = await github_token.replace(GOOD, session=_github())

        assert vault[("hive-mind", "CYPHER_GITHUB_TOKEN")] == GOOD
        assert any("could not be written" in line for line in state.configured)
