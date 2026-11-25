"""The host-facing API the core adds on top of the lifted bots.

Everything here is new to `hive-surfaces`: configuration arrives from the host
rather than from a sibling module, and a mind's own commands arrive through a
seam instead of a fork.
"""

import pytest

from hive_surfaces.config import SurfaceConfig, config, configure, photo_root


@pytest.fixture(autouse=True)
def _restore_config():
    before = config._installed()
    yield
    configure(before)


class TestConfigurationReachesAlreadyImportedCode:
    def test_a_handler_that_imported_config_sees_a_later_configure(self) -> None:
        """A surface binds `config` at import; the host configures afterwards."""
        from hive_surfaces.config import config as bound_at_import

        configure(SurfaceConfig(telegram_allowed_users=[991]))

        assert bound_at_import.telegram_allowed_users == [991]

    def test_an_unconfigured_core_authorizes_nobody(self) -> None:
        """The default must refuse every message, not answer everyone's."""
        configure(SurfaceConfig())

        assert config.telegram_allowed_users == []
        assert config.discord_allowed_users == []

    def test_overrides_apply_on_top_of_the_hosts_object(self) -> None:
        installed = configure(
            SurfaceConfig(default_model="m", telegram_allowed_users=[1]),
            default_model="other",
        )

        assert installed.default_model == "other"
        assert installed.telegram_allowed_users == [1]

    def test_the_photo_root_follows_the_configured_directory(self, tmp_path) -> None:
        """Never a path derived from the package's own location."""
        configure(SurfaceConfig(photo_dir=str(tmp_path / "shots")))

        root = photo_root()

        assert root == (tmp_path / "shots").resolve()
        assert root.is_dir()


class TestTelegramCommandSeam:
    @pytest.fixture(autouse=True)
    def _restore_table(self):
        import hive_surfaces.telegram_bot as tb

        extras, sealed = list(tb._EXTRA_COMMANDS), tb._TABLE_SEALED
        yield
        tb._EXTRA_COMMANDS[:] = extras
        tb._TABLE_SEALED = sealed

    def test_a_registered_command_joins_the_table_and_the_menu(self) -> None:
        import hive_surfaces.telegram_bot as tb

        async def cmd_triage(update, context):  # pragma: no cover - never called
            ...

        tb.register_command("triage", "Work the alert queue", cmd_triage)

        assert ("triage", "Work the alert queue", cmd_triage) in tb.all_commands()
        assert ("triage", "Work the alert queue") in tb.valid_menu_entries()

    def test_a_name_already_in_the_table_is_refused(self) -> None:
        import hive_surfaces.telegram_bot as tb

        with pytest.raises(ValueError):
            tb.register_command("sessions", "Shadow the picker", lambda *_: None)

    def test_registering_after_the_surface_is_built_raises(self) -> None:
        """A dropped registration is worse than a loud one."""
        import hive_surfaces.telegram_bot as tb

        tb._TABLE_SEALED = True

        with pytest.raises(tb.CommandsSealed):
            tb.register_command("triage", "Too late", lambda *_: None)

    def test_building_the_application_seals_the_table(self) -> None:
        from unittest.mock import MagicMock, patch

        import hive_surfaces.telegram_bot as tb

        with patch.object(tb, "ApplicationBuilder", MagicMock()):
            tb._build_application("token-never-valid")

        assert tb._TABLE_SEALED is True


class TestDiscordCommandSeam:
    def test_a_registered_command_joins_the_tree(self) -> None:
        import hive_surfaces.discord_bot as db

        async def cmd_triage(interaction):  # pragma: no cover - never called
            ...

        db.register_discord_command("triage", "Work the alert queue", cmd_triage)
        try:
            assert db.bot.tree.get_command("triage") is not None
        finally:
            db.bot.tree.remove_command("triage")

    def test_a_name_already_in_the_tree_is_refused(self) -> None:
        import hive_surfaces.discord_bot as db

        with pytest.raises(ValueError):
            db.register_discord_command("sessions", "Shadow the picker", lambda *_: None)
