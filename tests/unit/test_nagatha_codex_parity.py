"""Tests for Nagatha's Codex-local runtime assets."""

from pathlib import Path
import tomllib

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


REPO_ROOT = Path(__file__).resolve().parents[2]
NAGATHA_CODEX_HOME = REPO_ROOT / "minds" / "nagatha" / ".codex"

# Nagatha is a per-deployment mind (minds/* is gitignored); on a clone
# without her, there is nothing to guard.
pytestmark = pytest.mark.skipif(
    not NAGATHA_CODEX_HOME.exists(), reason="nagatha mind not present on this host"
)


def test_nagatha_codex_config_declares_agent_limits() -> None:
    config = tomllib.loads((NAGATHA_CODEX_HOME / "config.toml").read_text())

    assert config["agents"]["max_threads"] == 6
    assert config["agents"]["max_depth"] == 1





def test_nagatha_mind_fragment_sets_codex_home() -> None:
    """The Nagatha mind container is the one that needs CODEX_HOME, not the bot.

    Post-Phase-1 consolidation moved per-mind config into
    minds/<name>/container/compose.yaml.  The repo bind-mounts the project
    into the container, so CODEX_HOME points at the in-project codex dir
    directly rather than a separate volume mount.
    """
    fragment = (REPO_ROOT / "minds" / "nagatha" / "container" / "compose.yaml").read_text()

    assert "nagatha:" in fragment
    assert "CODEX_HOME=/usr/src/app/minds/nagatha/.codex" in fragment
