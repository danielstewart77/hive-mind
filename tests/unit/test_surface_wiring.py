"""What this stack hands the shared surfaces.

The core's allow-lists default to empty on purpose, so this wiring is the
whole difference between a bot that answers its owner and one that refuses
every message. Two fields are this stack's own shape rather than a value
copied across: the resident channels are derived from the scheduled skills
that post into them, and the state directory has to be per mind because
every surface container here mounts the same project directory.
"""

import pytest

from surfaces.config import state_dir, surface_config


class TestWhereASurfaceKeepsItsState:
    def test_two_minds_sharing_the_mount_do_not_share_a_claim_file(
        self, monkeypatch
    ) -> None:
        """The claims are what stop a tap Telegram redelivers from acting
        twice, and four surface containers mount one project directory."""
        monkeypatch.delenv("SURFACE_STATE_DIR", raising=False)
        monkeypatch.setenv("MIND_ID", "ada-uuid")
        ada = state_dir()
        monkeypatch.setenv("MIND_ID", "bob-uuid")

        assert state_dir() != ada

    def test_an_unassigned_surface_still_gets_a_directory(self, monkeypatch) -> None:
        """A missing MIND_ID must not resolve to the mount root."""
        monkeypatch.delenv("SURFACE_STATE_DIR", raising=False)
        monkeypatch.setenv("MIND_ID", "")

        assert state_dir().rstrip("/").endswith("unassigned")

    def test_the_deployment_may_name_it_instead(self, monkeypatch) -> None:
        monkeypatch.setenv("SURFACE_STATE_DIR", "/var/lib/surface")

        assert state_dir() == "/var/lib/surface"


class TestWhatTheSurfacesAreHanded:
    def test_the_resident_channels_arrive_as_a_resolver_not_a_list(self) -> None:
        """A channel added to a skill has to start working without a restart,
        and only the skill knows its own channel."""
        assert callable(surface_config().discord_task_channels)

    def test_the_state_directory_is_the_per_mind_one(self, monkeypatch) -> None:
        monkeypatch.delenv("SURFACE_STATE_DIR", raising=False)
        monkeypatch.setenv("MIND_ID", "ada-uuid")

        assert surface_config().state_dir == state_dir()

    def test_the_surface_is_told_where_to_poll_for_unsolicited_turns(
        self, monkeypatch
    ) -> None:
        """It runs in its own container, so it shares no memory with the
        backend holding them and cannot be handed one in process."""
        monkeypatch.setenv("MIND_BACKEND_URL", "http://ada:8420")

        assert surface_config().proactive_poll_url == "http://ada:8420"
