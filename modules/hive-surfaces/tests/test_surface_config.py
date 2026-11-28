"""What the host hands over, and what a second call to hand some of it over does.

The core's defaults authorize nobody on purpose, so the configuration call is
the thing standing between a mind and a bot that refuses every message. That
makes two questions load-bearing: a partial call must not undo an earlier one,
and the paths the surfaces write to must come from the host rather than from
wherever the package happens to be installed.
"""

import pytest

from hive_surfaces.config import SurfaceConfig, config, configure, installed, state_root


@pytest.fixture(autouse=True)
def _restore_config():
    before = config._installed()
    yield
    configure(before)


class TestInstallingConfiguration:
    def test_a_whole_config_object_replaces_what_was_installed(self) -> None:
        """A host handing over a config is stating all of it."""
        configure(SurfaceConfig(telegram_allowed_users=[1], default_model="first"))
        configure(SurfaceConfig(default_model="second"))

        assert installed().telegram_allowed_users == []
        assert installed().default_model == "second"

    def test_keywords_alone_leave_every_field_they_do_not_name(self) -> None:
        """Otherwise the second call blanks the allow-list to nobody.

        A host configures the allow-lists at boot and then names a model when
        the console changes one. With the keyword call building off a fresh
        default, that second call silently stops the bot answering its owner.
        """
        configure(SurfaceConfig(telegram_allowed_users=[7], telegram_owner_chat_id=7))

        configure(default_model="claude-opus-5")

        assert installed().telegram_allowed_users == [7]
        assert installed().telegram_owner_chat_id == 7
        assert installed().default_model == "claude-opus-5"

    def test_keywords_given_with_a_config_object_override_its_fields(self) -> None:
        configure(SurfaceConfig(default_model="from-object"), default_model="override")

        assert installed().default_model == "override"

    def test_an_already_imported_reader_sees_a_later_call(self) -> None:
        """`from ... import config` binds the object, not the module attribute."""
        configure(SurfaceConfig(telegram_owner_chat_id=404))

        assert config.telegram_owner_chat_id == 404


class TestWhereSurfaceStateIsKept:
    def test_the_state_directory_is_the_one_the_host_configured(self, tmp_path) -> None:
        """Not a path derived from the package's own location.

        Installed as a package, that path is inside site-packages — which the
        next reinstall wipes, forgetting every single-use claim the picker had
        taken, which is the 2026-09-17 double-tap incident again.
        """
        configure(SurfaceConfig(state_dir=str(tmp_path / "surface-state")))

        assert state_root() == (tmp_path / "surface-state").resolve()

    def test_the_state_directory_exists_once_it_has_been_asked_for(self, tmp_path) -> None:
        target = tmp_path / "made" / "on" / "demand"
        configure(SurfaceConfig(state_dir=str(target)))

        assert state_root().is_dir()

    def test_the_picker_claim_file_lands_under_it(self, tmp_path, monkeypatch) -> None:
        """The claims are the reason the directory has to be the host's."""
        monkeypatch.delenv("PICKER_STATE_PATH", raising=False)
        configure(SurfaceConfig(state_dir=str(tmp_path / "state")))
        from hive_surfaces import bot_utils

        assert bot_utils._state_path().parent == (tmp_path / "state").resolve()

    def test_an_explicit_override_wins_over_the_configured_directory(
        self, tmp_path, monkeypatch
    ) -> None:
        """A test aims it at a temp file; so does a host injecting one path."""
        monkeypatch.setenv("PICKER_STATE_PATH", str(tmp_path / "elsewhere.json"))
        configure(SurfaceConfig(state_dir=str(tmp_path / "state")))
        from hive_surfaces import bot_utils

        assert bot_utils._state_path() == tmp_path / "elsewhere.json"
